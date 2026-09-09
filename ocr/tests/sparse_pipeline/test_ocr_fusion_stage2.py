from __future__ import annotations

import hashlib
import io
import inspect
import math
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.block_crops import BlockCropPair, BlockCropper
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningMode,
    BlockSetAlgebra,
    MembershipUnit,
    MembershipUnitKind,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import Box, Segment, SegmentKind
from app.sparse_pipeline.ocr_fusion import (
    EditKind,
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionInvariantError,
    OcrFusionLimitError,
    OcrFusionStatus,
    OcrRoutingMode,
    OcrReplicaConflict,
    OverlapConsensus,
    SegmentObservation,
    _AlignmentBudget,
    align_exact_text,
    compact_ocr_text,
    exact_script_scores,
)
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrAttributionStatus,
    OcrOutputGeometry,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrQueueStatus,
    OcrResource,
    OcrTransform,
    OcrWord,
)
from app.sparse_pipeline.runtime import resolve_sparse_runtime_profile


def _segment(segment_id: str, bbox: Box, row: int) -> Segment:
    return Segment(
        segment_id=segment_id,
        bbox=bbox,
        source_bbox=bbox,
        kind=SegmentKind.TEXT,
        ink_pixels=max(1, bbox.area // 4),
        row_index=row,
        order_key=(row, 0),
        parent_path=("root",),
        component_ids=(row,),
    )


def _png_bytes(pixels: np.ndarray) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(pixels, mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _crops(
    plan: BlockPlan,
    *,
    identical_blocks: bool = False,
) -> tuple[BlockCropPair, ...]:
    width, height = plan.aligned_size
    if identical_blocks:
        pixels = np.full((height, width, 3), 241, dtype=np.uint8)
    else:
        rng = np.random.default_rng(20260721 + width * 17 + height)
        pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    page = CropInput("fusion-page", _png_bytes(pixels))
    return BlockCropper().crop(page, aligned_size=plan.aligned_size, plan=plan)


def _overlap_fixture(
    *,
    identical_blocks: bool = False,
) -> tuple[BlockPlan, tuple[Segment, ...], tuple[BlockCropPair, ...]]:
    segments = (
        _segment("segment-000000", Box(0, 0, 100, 10), 0),
        _segment("segment-000001", Box(0, 10, 100, 20), 1),
        _segment("segment-000002", Box(0, 20, 100, 30), 2),
    )
    blocks = (
        RecognitionBlock(
            block_id="block-000000",
            bbox=Box(0, 0, 100, 20),
            core_segment_ids=("segment-000000", "segment-000001"),
            segment_ids=("segment-000000", "segment-000001"),
            context_segment_ids=(),
            object_ids=("object-000000",),
        ),
        RecognitionBlock(
            block_id="block-000001",
            bbox=Box(0, 10, 100, 30),
            core_segment_ids=("segment-000002",),
            segment_ids=("segment-000001", "segment-000002"),
            context_segment_ids=("segment-000001",),
            object_ids=("object-000001",),
        ),
    )
    algebra = BlockSetAlgebra(
        first_block_id="block-000000",
        second_block_id="block-000001",
        intersection_segment_ids=("segment-000001",),
        union_segment_ids=(
            "segment-000000",
            "segment-000001",
            "segment-000002",
        ),
        xor_segment_ids=("segment-000000", "segment-000002"),
        first_only_segment_ids=("segment-000000",),
        second_only_segment_ids=("segment-000002",),
    )
    plan = BlockPlan(
        aligned_size=(100, 30),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=blocks,
        adjacent_algebra=(algebra,),
    )
    return plan, segments, _crops(plan, identical_blocks=identical_blocks)


def _single_segment_fixture() -> tuple[BlockPlan, tuple[Segment, ...], tuple[BlockCropPair, ...]]:
    segments = (_segment("segment-000000", Box(0, 0, 32, 12), 0),)
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 32, 12),
        core_segment_ids=("segment-000000",),
        segment_ids=("segment-000000",),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(32, 12),
        source_segment_ids=("segment-000000",),
        blocks=(block,),
        adjacent_algebra=(),
    )
    return plan, segments, _crops(plan)


def _three_block_fixture() -> tuple[BlockPlan, tuple[Segment, ...], tuple[BlockCropPair, ...]]:
    segments = tuple(
        _segment(
            f"segment-{index:06d}",
            Box(0, index * 10, 80, (index + 1) * 10),
            index,
        )
        for index in range(4)
    )
    blocks = (
        RecognitionBlock(
            "block-000000",
            Box(0, 0, 80, 20),
            ("segment-000000", "segment-000001"),
            ("segment-000000", "segment-000001"),
            (),
            ("object-000000",),
        ),
        RecognitionBlock(
            "block-000001",
            Box(0, 10, 80, 30),
            ("segment-000002",),
            ("segment-000001", "segment-000002"),
            ("segment-000001",),
            ("object-000001",),
        ),
        RecognitionBlock(
            "block-000002",
            Box(0, 20, 80, 40),
            ("segment-000003",),
            ("segment-000002", "segment-000003"),
            ("segment-000002",),
            ("object-000002",),
        ),
    )
    algebra = (
        BlockSetAlgebra(
            "block-000000",
            "block-000001",
            ("segment-000001",),
            ("segment-000000", "segment-000001", "segment-000002"),
            ("segment-000000", "segment-000002"),
            ("segment-000000",),
            ("segment-000002",),
        ),
        BlockSetAlgebra(
            "block-000001",
            "block-000002",
            ("segment-000002",),
            ("segment-000001", "segment-000002", "segment-000003"),
            ("segment-000001", "segment-000003"),
            ("segment-000001",),
            ("segment-000003",),
        ),
    )
    plan = BlockPlan(
        aligned_size=(80, 40),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=blocks,
        adjacent_algebra=algebra,
    )
    return plan, segments, _crops(plan)


def _orthogonal_membership_fixture() -> tuple[BlockPlan, tuple[Segment, ...], tuple[BlockCropPair, ...]]:
    """A 2x3 matrix encoded by two row windows and one column probe."""

    segments = tuple(
        _segment(
            f"segment-{index:06d}",
            Box(
                (index % 2) * 20,
                (index // 2) * 12,
                (index % 2 + 1) * 20,
                (index // 2 + 1) * 12,
            ),
            index // 2,
        )
        for index in range(6)
    )
    blocks = (
        RecognitionBlock(
            "block-000000",
            Box(0, 0, 40, 24),
            ("segment-000000", "segment-000001"),
            (
                "segment-000000",
                "segment-000001",
                "segment-000002",
                "segment-000003",
            ),
            ("segment-000002", "segment-000003"),
            ("object-000000",),
            "scope-000000",
        ),
        RecognitionBlock(
            "block-000001",
            Box(0, 12, 40, 36),
            (
                "segment-000002",
                "segment-000003",
                "segment-000004",
                "segment-000005",
            ),
            (
                "segment-000002",
                "segment-000003",
                "segment-000004",
                "segment-000005",
            ),
            (),
            ("object-000000",),
            "scope-000000",
        ),
        RecognitionBlock(
            "block-000002",
            Box(0, 0, 20, 36),
            (),
            ("segment-000000", "segment-000002", "segment-000004"),
            ("segment-000000", "segment-000002", "segment-000004"),
            (),
            "scope-000000",
        ),
    )

    def algebra(first_index: int, second_index: int) -> BlockSetAlgebra:
        first = set(blocks[first_index].segment_ids)
        second = set(blocks[second_index].segment_ids)
        canonical = tuple(item.segment_id for item in segments)

        def ordered(values: set[str]) -> tuple[str, ...]:
            return tuple(item for item in canonical if item in values)

        return BlockSetAlgebra(
            blocks[first_index].block_id,
            blocks[second_index].block_id,
            ordered(first & second),
            ordered(first | second),
            ordered(first ^ second),
            ordered(first - second),
            ordered(second - first),
        )

    plan = BlockPlan(
        aligned_size=(40, 36),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=blocks,
        adjacent_algebra=(algebra(0, 1), algebra(0, 2), algebra(1, 2)),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=tuple(
            MembershipUnit(
                f"membership-unit-{index:06d}",
                MembershipUnitKind.SEGMENT,
                (segment.segment_id,),
                tuple(block.block_id for block in blocks if segment.segment_id in block.segment_ids),
                "scope-000000",
            )
            for index, segment in enumerate(segments)
        ),
        matrix_sha256="0" * 64,
    )
    return plan, segments, _crops(plan)


def _word(
    segment: Segment,
    block: RecognitionBlock,
    text: str,
    confidence: float,
) -> OcrWord:
    return OcrWord(
        text=text,
        bbox=Box(
            segment.bbox.left - block.bbox.left,
            segment.bbox.top - block.bbox.top,
            segment.bbox.right - block.bbox.left,
            segment.bbox.bottom - block.bbox.top,
        ),
        confidence=confidence,
    )


def _complete_job(
    index: int,
    *,
    block: RecognitionBlock,
    transform: OcrTransform,
    lane_id: str,
    words: tuple[OcrWord, ...] = (),
    engine_text: str | None = None,
) -> OcrJobResult:
    resolved_text = " ".join(word.text for word in words) if engine_text is None else engine_text
    return OcrJobResult(
        job_id=f"ocr-job-{index:08d}",
        block_id=block.block_id,
        transform=transform,
        lane_id=lane_id,
        resource=OcrResource.GPU if lane_id.startswith("gpu") else OcrResource.CPU,
        status=OcrJobStatus.COMPLETE,
        output=OcrEngineOutput(
            text=resolved_text,
            words=words,
            geometry=(OcrOutputGeometry.TEXT_ONLY if resolved_text and not words else OcrOutputGeometry.WORD_BOXES),
        ),
        error_type=None,
        error_message=None,
        elapsed_seconds=0.01,
    )


def _failed_job(
    index: int,
    *,
    block: RecognitionBlock,
    transform: OcrTransform,
    lane_id: str,
) -> OcrJobResult:
    return OcrJobResult(
        job_id=f"ocr-job-{index:08d}",
        block_id=block.block_id,
        transform=transform,
        lane_id=lane_id,
        resource=OcrResource.CPU,
        status=OcrJobStatus.FAILED,
        output=None,
        error_type="RuntimeError",
        error_message="observed worker failure",
        elapsed_seconds=0.01,
    )


def _queue(*jobs: OcrJobResult) -> OcrQueueResult:
    complete = sum(job.status is OcrJobStatus.COMPLETE for job in jobs)
    failed = len(jobs) - complete
    return OcrQueueResult(
        jobs=tuple(jobs),
        status=(OcrQueueStatus.COMPLETE if failed == 0 else OcrQueueStatus.PARTIAL),
        complete=complete,
        failed=failed,
    )


def _matrix_queue(
    plan: BlockPlan,
    crops: tuple[BlockCropPair, ...],
    *specified_jobs: OcrJobResult,
) -> OcrQueueResult:
    lane_ids = tuple(dict.fromkeys(job.lane_id for job in specified_jobs))
    if not lane_ids:
        return _queue()
    specified: dict[tuple[str, OcrTransform, str], OcrJobResult] = {}
    for job in specified_jobs:
        key = (job.block_id, job.transform, job.lane_id)
        assert key not in specified
        specified[key] = job
    crop_by_id = {crop.block_id: crop for crop in crops}
    lane_contract = {lane_id: next(job for job in specified_jobs if job.lane_id == lane_id) for lane_id in lane_ids}
    jobs: list[OcrJobResult] = []
    for block in plan.blocks:
        crop = crop_by_id[block.block_id]
        context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
            payload = crop.raw.png_bytes if transform is OcrTransform.RAW else crop.gamma.png_bytes
            input_sha256 = hashlib.sha256(payload).hexdigest()
            for lane_id in lane_ids:
                key = (block.block_id, transform, lane_id)
                contract = lane_contract[lane_id]
                job = specified.get(key)
                if job is None:
                    job = _failed_job(
                        0,
                        block=block,
                        transform=transform,
                        lane_id=lane_id,
                    )
                jobs.append(
                    replace(
                        job,
                        job_id=f"ocr-job-{len(jobs):08d}",
                        block_id=block.block_id,
                        transform=transform,
                        lane_id=lane_id,
                        resource=contract.resource,
                        input_sha256=input_sha256,
                        context_sha256=context_sha256,
                        capability_id=contract.capability_id,
                    )
                )
    return _queue(*jobs)


def _evidence_job(
    index: int,
    *,
    plan: BlockPlan,
    segments: tuple[Segment, ...],
    block_index: int,
    transform: OcrTransform,
    lane_id: str,
    evidence: dict[str, tuple[str, float]],
) -> OcrJobResult:
    block = plan.blocks[block_index]
    by_id = {segment.segment_id: segment for segment in segments}
    words = tuple(
        _word(by_id[segment_id], block, text, confidence)
        for segment_id in block.segment_ids
        if segment_id in evidence
        for text, confidence in (evidence[segment_id],)
    )
    return _complete_job(
        index,
        block=block,
        transform=transform,
        lane_id=lane_id,
        words=words,
    )


def _fused(result: object, segment_id: str) -> object:
    segments = getattr(result, "segments")
    return next(item for item in segments if item.segment_id == segment_id)


def _fuse_raw_context_votes(
    votes: tuple[tuple[str, float], ...],
):
    fusion = OcrEvidenceFusion()
    observations = tuple(
        SegmentObservation(
            observation_id=f"observation-{index}",
            job_id=f"job-{index}",
            segment_id="segment-target",
            block_id=f"block-{index}",
            transform=OcrTransform.RAW,
            lane_id="cpu",
            capability_id="cap-shared",
            text=text,
            confidence=confidence,
            page_bboxes=(Box(0, 0, 10, 10),),
            input_sha256=f"{index + 100:064x}",
            context_sha256=f"{index + 1:064x}",
            source_replica_conflict=False,
        )
        for index, (text, confidence) in enumerate(votes)
    )
    return fusion._fuse_segment(
        "segment-target",
        observations,
        budget=_AlignmentBudget(fusion.config),
    )


def test_lost_metric_removes_whitespace_then_counts_exact_positional_edits() -> None:
    alignment = align_exact_text("a b\nc", "aXc !")

    assert (alignment.left, alignment.right) == ("abc", "aXc!")
    assert alignment.distance == 2
    assert alignment.similarity == 0.5
    assert tuple(
        (
            operation.kind,
            operation.left_index,
            operation.right_index,
            operation.left_character,
            operation.right_character,
        )
        for operation in alignment.operations
    ) == (
        (EditKind.MATCH, 0, 0, "a", "a"),
        (EditKind.SUBSTITUTE, 1, 1, "b", "X"),
        (EditKind.MATCH, 2, 2, "c", "c"),
        (EditKind.INSERT, None, 3, None, "!"),
    )


@pytest.mark.parametrize(
    ("left", "right", "distance"),
    (
        ("A", "a", 1),
        ("A", "А", 1),  # Latin A and Cyrillic A stay distinct.
        ("é", "e\u0301", 2),
        ("１２", "12", 2),
    ),
)
def test_alignment_never_casefolds_normalizes_or_repairs_confusables(
    left: str,
    right: str,
    distance: int,
) -> None:
    alignment = align_exact_text(left, right)
    assert alignment.distance == distance
    assert alignment.left == left
    assert alignment.right == right


def test_compaction_removes_unicode_whitespace_only_and_preserves_codepoints() -> None:
    assert compact_ocr_text(" Пр\tивет\nA\u00a0中 ") == "ПриветA中"
    assert compact_ocr_text("A-А") == "A-А"


def test_phrase_with_multiple_significant_intersections_is_preserved_unassigned() -> None:
    segments = (
        _segment("segment-000000", Box(0, 0, 60, 20), 0),
        _segment("segment-000001", Box(40, 0, 100, 20), 1),
    )
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 100, 20),
        core_segment_ids=tuple(item.segment_id for item in segments),
        segment_ids=tuple(item.segment_id for item in segments),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(100, 20),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crops = _crops(plan)
    job = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(OcrWord("two segments", Box(30, 0, 70, 20), 0.9),),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert tuple(item.text for item in result.observations) == ("", "")
    assert len(result.unassigned_word_observations) == 1
    unassigned = result.unassigned_word_observations[0]
    assert unassigned.text == "two segments"
    assert unassigned.reason == "ambiguous-segment-intersection"
    assert unassigned.page_bbox == Box(30, 0, 70, 20)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_word_crossing_line_boundary_routes_to_unique_center_owner() -> None:
    segments = (
        _segment("segment-000000", Box(0, 0, 100, 16), 0),
        _segment("segment-000001", Box(0, 16, 100, 32), 1),
    )
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 100, 32),
        core_segment_ids=tuple(item.segment_id for item in segments),
        segment_ids=tuple(item.segment_id for item in segments),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(100, 32),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crops = _crops(plan)
    jobs = tuple(
        _complete_job(
            index,
            block=block,
            transform=transform,
            lane_id="cpu",
            words=(
                OcrWord("context", Box(20, 3, 80, 18), 0.9),
                OcrWord("line-two", Box(20, 19, 80, 30), 0.9),
            ),
        )
        for index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert tuple(item.text for item in result.observations) == (
        "context",
        "line-two",
        "context",
        "line-two",
    )
    assert result.unassigned_word_observations == ()
    assert result.status is OcrFusionStatus.COMPLETE


def test_word_without_any_segment_intersection_is_preserved_unassigned() -> None:
    segments = (_segment("segment-000000", Box(0, 0, 20, 20), 0),)
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 100, 20),
        core_segment_ids=("segment-000000",),
        segment_ids=("segment-000000",),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(100, 20),
        source_segment_ids=("segment-000000",),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crops = _crops(plan)
    job = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(OcrWord("outside", Box(70, 0, 90, 20), 0.8),),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert result.observations[0].text == ""
    assert len(result.unassigned_word_observations) == 1
    unassigned = result.unassigned_word_observations[0]
    assert unassigned.text == "outside"
    assert unassigned.reason == "no-segment-intersection"
    assert unassigned.page_bbox == Box(70, 0, 90, 20)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_words_on_one_visual_line_are_ordered_by_x_not_box_top() -> None:
    segments = (_segment("segment-000000", Box(0, 0, 120, 24), 0),)
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 120, 24),
        core_segment_ids=("segment-000000",),
        segment_ids=("segment-000000",),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(120, 24),
        source_segment_ids=("segment-000000",),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crops = _crops(plan)
    job = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(
            OcrWord("Second", Box(34, 2, 78, 20), 0.9),
            OcrWord("item", Box(82, 1, 112, 20), 0.9),
            OcrWord("1.", Box(4, 5, 22, 19), 0.9),
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert result.observations[0].text == "1. Second item"


def test_routed_words_are_line_major_before_left_to_right() -> None:
    segments = (_segment("segment-000000", Box(0, 0, 120, 48), 0),)
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 120, 48),
        core_segment_ids=("segment-000000",),
        segment_ids=("segment-000000",),
        context_segment_ids=(),
        object_ids=("object-000000",),
    )
    plan = BlockPlan(
        aligned_size=(120, 48),
        source_segment_ids=("segment-000000",),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crops = _crops(plan)
    job = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(
            OcrWord("right-one", Box(65, 1, 116, 18), 0.9),
            OcrWord("left-two", Box(3, 27, 55, 45), 0.9),
            OcrWord("left-one", Box(3, 4, 56, 21), 0.9),
            OcrWord("right-two", Box(64, 25, 116, 43), 0.9),
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert result.observations[0].text == ("left-one right-one left-two right-two")


def test_text_only_output_is_unattributable_without_fake_segment_observations() -> None:
    plan, segments, crops = _overlap_fixture()
    job = _complete_job(
        0,
        block=plan.blocks[0],
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(),
        engine_text="unroutable text without word boxes",
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert result.observations == ()
    assert len(result.block_text_observations) == 1
    block_text = result.block_text_observations[0]
    assert block_text.text == "unroutable text without word boxes"
    assert block_text.attribution_status is OcrAttributionStatus.UNATTRIBUTABLE
    assert block_text.attribution_reason == "no-observed-bbox"
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_observed_medoid_beats_one_high_confidence_outlier() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = (
        replace(
            _evidence_job(
                0,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu-a",
                evidence={target: ("ПриветAB中文", 0.72)},
            ),
            capability_id="cap-correct",
        ),
        replace(
            _evidence_job(
                1,
                plan=plan,
                segments=segments,
                block_index=1,
                transform=OcrTransform.RAW,
                lane_id="cpu-a",
                evidence={target: ("ПриветAB中文", 0.70)},
            ),
            capability_id="cap-correct",
        ),
        replace(
            _evidence_job(
                2,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu-outlier",
                evidence={target: ("ПpивeтA8中囯", 0.99)},
            ),
            capability_id="cap-outlier",
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert fused.selected_text == "ПриветAB中文"
    assert fused.selected_lane_id == "cpu-a"
    assert dict(fused.script_scores) == {
        "cjk": pytest.approx(0.2),
        "cyrillic": pytest.approx(0.6),
        "latin": pytest.approx(0.2),
    }


def test_existing_clean_raw_beats_repeated_short_script_confusable() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = (
        replace(
            _evidence_job(
                0,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu-a",
                evidence={target: ("Инструмент запущен, Ho без", 0.955)},
            ),
            capability_id="cap-a",
        ),
        replace(
            _evidence_job(
                1,
                plan=plan,
                segments=segments,
                block_index=1,
                transform=OcrTransform.RAW,
                lane_id="cpu-a",
                evidence={target: ("Инструмент запущен, Ho без", 0.955)},
            ),
            capability_id="cap-a",
        ),
        replace(
            _evidence_job(
                2,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu-clean",
                evidence={target: ("Инструмент запущен, но без", 0.956)},
            ),
            capability_id="cap-clean",
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert _fused(result, target).selected_text == ("Инструмент запущен, но без")


def test_capability_replicas_cannot_outvote_two_independent_capabilities() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    replicas = tuple((f"replica-{index}", "cap-a", "wrong") for index in range(5))
    independent = (
        ("independent-b", "cap-b", "right"),
        ("independent-c", "cap-c", "right"),
    )
    jobs: list[OcrJobResult] = []
    for block_index in (0, 1):
        for lane_id, capability_id, text in replicas + independent:
            job = _evidence_job(
                len(jobs),
                plan=plan,
                segments=segments,
                block_index=block_index,
                transform=OcrTransform.RAW,
                lane_id=lane_id,
                evidence={target: (text, 0.9)},
            )
            jobs.append(replace(job, capability_id=capability_id))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert fused.observation_count == 14
    assert fused.selected_text == "right"
    assert fused.selected_lane_id in {"independent-b", "independent-c"}


def test_complete_queue_word_bbox_outside_bound_crop_is_rejected() -> None:
    plan, segments, crops = _single_segment_fixture()
    block = plan.blocks[0]
    outside = OcrWord(
        text="outside",
        bbox=Box(0, 0, block.bbox.width + 1, block.bbox.height),
        confidence=0.9,
    )
    raw = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(outside,),
    )
    gamma = _complete_job(
        1,
        block=block,
        transform=OcrTransform.GAMMA,
        lane_id="cpu",
        words=(_word(segments[0], block, "inside", 0.9),),
    )
    complete_queue = _matrix_queue(plan, crops, raw, gamma)
    assert complete_queue.status is OcrQueueStatus.COMPLETE

    with pytest.raises(OcrFusionInvariantError, match="outside.*crop"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=complete_queue,
        )


def test_canonical_slot_words_use_declared_canvas_and_overflow_fails_closed() -> None:
    plan, _segments, _original_crops = _single_segment_fixture()
    raster_block = replace(
        plan.blocks[0],
        bbox=Box(0, 0, 720, 1406),
    )
    raster_plan = replace(
        plan,
        aligned_size=(720, 1406),
        blocks=(raster_block,),
    )
    raster_crop = _crops(raster_plan)[0]
    block = replace(plan.blocks[0])
    object.__setattr__(block, "matrix_window_kind", "polar-local-full")
    plan = replace(plan, blocks=(block,))
    metadata_crop = object.__new__(BlockCropPair)
    for field_name in BlockCropPair.__dataclass_fields__:
        object.__setattr__(
            metadata_crop,
            field_name,
            block.bbox if field_name == "bbox" else getattr(raster_crop, field_name),
        )
    crops = (metadata_crop,)
    doc_course_word = OcrWord(
        text="membership-86",
        bbox=Box(86, 0, 87, 1),
        confidence=0.99,
    )
    jobs = tuple(
        _complete_job(
            index,
            block=block,
            transform=transform,
            lane_id="cpu",
            words=(doc_course_word,),
        )
        for index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )
    queue = replace(
        _matrix_queue(plan, crops, *jobs),
        diagnostics=("geometry=canonical-membership-slots-v1;" "width=720;height=1406",),
    )
    fusion = OcrEvidenceFusion()

    fusion._validate_sparse_jobs(plan=plan, crops=crops, queue=queue)

    legacy_v53_queue = replace(
        queue,
        diagnostics=("block=block-000000;geometry=canonical-membership-slots-v1",),
    )
    fusion._validate_sparse_jobs(
        plan=plan,
        crops=crops,
        queue=legacy_v53_queue,
    )

    oversized_canvas_queue = replace(
        queue,
        diagnostics=("geometry=canonical-membership-slots-v1;" "width=721;height=1406",),
    )
    with pytest.raises(
        OcrFusionInvariantError,
        match="slot canvas exceeds bound crop raster",
    ):
        fusion._validate_sparse_jobs(
            plan=plan,
            crops=crops,
            queue=oversized_canvas_queue,
        )

    overflow = replace(
        doc_course_word,
        bbox=Box(719, 0, 721, 1),
    )
    overflow_jobs = tuple(
        replace(
            job,
            output=replace(
                job.output,
                text=overflow.text,
                words=(overflow,),
            ),
        )
        for job in queue.jobs
    )
    overflow_queue = replace(queue, jobs=overflow_jobs)
    with pytest.raises(OcrFusionInvariantError, match="outside.*crop"):
        fusion._validate_sparse_jobs(
            plan=plan,
            crops=crops,
            queue=overflow_queue,
        )


def test_same_source_text_with_conflicting_bbox_attribution_is_unresolved() -> None:
    plan, segments, crops = _overlap_fixture()
    block = plan.blocks[0]
    first = replace(
        _complete_job(
            0,
            block=block,
            transform=OcrTransform.RAW,
            lane_id="replica-a",
            words=(_word(segments[0], block, "same", 0.9),),
        ),
        capability_id="cap-replica",
    )
    second = replace(
        _complete_job(
            1,
            block=block,
            transform=OcrTransform.RAW,
            lane_id="replica-b",
            words=(_word(segments[1], block, "same", 0.9),),
        ),
        capability_id="cap-replica",
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, first, second),
    )

    assert len(result.replica_conflicts) == 1
    conflict = result.replica_conflicts[0]
    assert isinstance(conflict, OcrReplicaConflict)
    assert conflict.capability_id == "cap-replica"
    assert conflict.transform is OcrTransform.RAW
    assert len(conflict.evidence_sha256) == 2
    for segment_id in ("segment-000000", "segment-000001"):
        fused = _fused(result, segment_id)
        assert "capability_replica_conflict" in fused.uncertainty_reasons
        assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_same_source_bbox_and_text_with_conflicting_confidence_is_unresolved() -> None:
    plan, segments, crops = _single_segment_fixture()
    block = plan.blocks[0]
    jobs = tuple(
        replace(
            _complete_job(
                index,
                block=block,
                transform=OcrTransform.RAW,
                lane_id=f"replica-{index}",
                words=(_word(segments[0], block, "same", confidence),),
            ),
            capability_id="cap-replica",
        )
        for index, confidence in enumerate((0.1, 0.99))
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = result.segments[0]

    assert len(result.replica_conflicts) == 1
    assert isinstance(result.replica_conflicts[0], OcrReplicaConflict)
    assert len(result.replica_conflicts[0].evidence_sha256) == 2
    assert "capability_replica_conflict" in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_distinct_text_capability_clones_are_one_vote_and_block_resolution() -> None:
    plan, segments, crops = _single_segment_fixture()
    target = "segment-000000"
    jobs: list[OcrJobResult] = []
    for clone_index in range(20):
        clone = _evidence_job(
            len(jobs),
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id=f"clone-{clone_index:02d}",
            evidence={target: (f"WRONG-{clone_index:02d}", 0.99)},
        )
        jobs.append(replace(clone, capability_id="cap-cloned"))
    for lane_id, capability_id in (
        ("right-a", "cap-right-a"),
        ("right-b", "cap-right-b"),
    ):
        right = _evidence_job(
            len(jobs),
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id=lane_id,
            evidence={target: ("RIGHT", 0.9)},
        )
        jobs.append(replace(right, capability_id=capability_id))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert len(result.observations) == 22
    assert fused.observation_count == 22
    assert len(fused.alignments) == 22
    assert fused.independent_context_count == 1
    assert fused.selected_text == "RIGHT"
    assert fused.selected_lane_id in {"right-a", "right-b"}
    selected = next(item for item in result.observations if item.observation_id == fused.selected_observation_id)
    assert selected.capability_id in {"cap-right-a", "cap-right-b"}
    assert "capability_replica_conflict" in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_stable_selected_capability_raw_is_not_overridden_by_other_capability_noise() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs: list[OcrJobResult] = []
    for block_index in (0, 1):
        for transform, text in (
            (OcrTransform.RAW, "stable-raw"),
            (OcrTransform.GAMMA, "gamma-disagrees"),
        ):
            job = _evidence_job(
                len(jobs),
                plan=plan,
                segments=segments,
                block_index=block_index,
                transform=transform,
                lane_id="selected-capability",
                evidence={target: (text, 0.9)},
            )
            jobs.append(replace(job, capability_id="cap-stable"))
        noisy = _evidence_job(
            len(jobs),
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="other-capability",
            evidence={target: (f"noise-{block_index}", 0.9)},
        )
        jobs.append(replace(noisy, capability_id="cap-noisy"))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert fused.selected_text == "stable-raw"
    assert fused.selected_transform is OcrTransform.RAW
    assert fused.selected_lane_id == "selected-capability"
    assert "gamma_stable_override" not in fused.uncertainty_reasons
    assert "transform_conflict" in fused.uncertainty_reasons


def test_script_scores_ignore_punctuation_and_count_greek_exactly() -> None:
    scores = dict(exact_script_scores("Αλφα, Привет! ABC 123…"))
    assert scores == {
        "cyrillic": pytest.approx(6 / 13),
        "greek": pytest.approx(4 / 13),
        "latin": pytest.approx(3 / 13),
    }
    assert exact_script_scores("—, 123!? …") == ()


def _run_gamma_guard(
    gamma: tuple[tuple[int, str, str, float], ...],
    *,
    include_raw: bool = True,
    raw_confidence: float = 0.8,
) -> object:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs: list[OcrJobResult] = []
    if include_raw:
        for block_index in (0, 1):
            jobs.append(
                replace(
                    _evidence_job(
                        len(jobs),
                        plan=plan,
                        segments=segments,
                        block_index=block_index,
                        transform=OcrTransform.RAW,
                        lane_id="cpu-raw",
                        evidence={target: ("raw-control", raw_confidence)},
                    ),
                    capability_id="cap-shared",
                )
            )
    for block_index, lane_id, text, confidence in gamma:
        jobs.append(
            replace(
                _evidence_job(
                    len(jobs),
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=OcrTransform.GAMMA,
                    lane_id=lane_id,
                    evidence={target: (text, confidence)},
                ),
                capability_id="cap-shared",
            )
        )
    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    return _fused(result, target)


def test_gamma_guard_admits_exact_two_context_evidence_at_raw_confidence() -> None:
    fused = _run_gamma_guard(
        (
            (0, "gpu", "gamma-better", 0.8),
            (1, "gpu", "gamma-better", 0.8),
        )
    )
    assert fused.selected_text == "raw-control"
    assert fused.selected_transform is OcrTransform.RAW
    assert "transform_conflict" in fused.uncertainty_reasons
    assert fused.unresolved is True


def test_gamma_consensus_counts_contexts_across_different_lanes() -> None:
    fused = _run_gamma_guard(
        (
            (0, "gpu-a", "gamma-better", 0.9),
            (1, "gpu-b", "gamma-better", 0.9),
        )
    )
    assert fused.selected_text == "raw-control"
    assert fused.selected_transform is OcrTransform.RAW
    assert "transform_conflict" in fused.uncertainty_reasons


@pytest.mark.parametrize(
    "gamma",
    (
        (
            (0, "gpu-a", "single", 0.95),
            (0, "gpu-b", "single", 0.95),
        ),
        (
            (0, "gpu", "unstable-a", 0.95),
            (1, "gpu", "unstable-b", 0.95),
        ),
        (
            (0, "gpu", "low", 0.79),
            (1, "gpu", "low", 0.79),
        ),
    ),
)
def test_gamma_guard_rejects_duplicate_context_unstable_or_lower_confidence(
    gamma: tuple[tuple[int, str, str, float], ...],
) -> None:
    fused = _run_gamma_guard(gamma)
    assert fused.selected_text == "raw-control"
    assert fused.selected_transform is OcrTransform.RAW


def test_gamma_without_word_boxes_cannot_replace_raw_control() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = [
        _evidence_job(
            index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={target: ("raw-control", 0.7)},
        )
        for index, block_index in enumerate((0, 1))
    ]
    for block_index in (0, 1):
        jobs.append(
            _complete_job(
                len(jobs),
                block=plan.blocks[block_index],
                transform=OcrTransform.GAMMA,
                lane_id="gpu",
                words=(),
                engine_text="gamma-without-boxes",
            )
        )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    assert fused.selected_text == "raw-control"
    assert fused.selected_transform is OcrTransform.RAW


def test_gamma_is_fail_closed_without_any_raw_control() -> None:
    fused = _run_gamma_guard(
        (
            (0, "gpu-a", "unsupported", 0.99),
            (1, "gpu-b", "unsupported", 0.99),
        ),
        include_raw=False,
    )
    assert fused.selected_text is None
    assert fused.selected_transform is None
    assert "no_raw_control" in fused.uncertainty_reasons


def test_failed_queue_job_does_not_erase_completed_evidence() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    failed = _failed_job(
        0,
        block=plan.blocks[0],
        transform=OcrTransform.RAW,
        lane_id="cpu-failed",
    )
    completed = _evidence_job(
        1,
        plan=plan,
        segments=segments,
        block_index=0,
        transform=OcrTransform.RAW,
        lane_id="cpu-good",
        evidence={target: ("survives", 0.9)},
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, failed, completed),
    )
    fused = _fused(result, target)
    assert fused.selected_text == "survives"
    assert fused.observation_count == 1
    assert all(item.lane_id == completed.lane_id for item in result.observations)


def test_geometry_or_xor_are_copied_exactly_without_text_subtraction() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000000": ("same", 0.9),
                "segment-000001": ("same", 0.9),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000001": ("same", 0.9),
                "segment-000002": ("same", 0.9),
            },
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    overlap = result.overlaps[0]

    assert overlap.intersection_segment_ids == ("segment-000001",)
    assert overlap.union_segment_ids == (
        "segment-000000",
        "segment-000001",
        "segment-000002",
    )
    assert overlap.xor_segment_ids == ("segment-000000", "segment-000002")
    assert overlap.observed_union_segment_ids == overlap.union_segment_ids
    assert overlap.observed_xor_segment_ids == overlap.xor_segment_ids
    assert result.status is OcrFusionStatus.COMPLETE


def test_observed_or_xor_evidence_is_scoped_to_each_adjacent_block_pair() -> None:
    plan, segments, crops = _three_block_fixture()
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000000": ("zero", 0.9),
                "segment-000001": ("one", 0.9),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={"segment-000001": ("one", 0.9)},
        ),
        _evidence_job(
            2,
            plan=plan,
            segments=segments,
            block_index=2,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000002": ("two", 0.9),
                "segment-000003": ("three", 0.9),
            },
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    first_pair, second_pair = result.overlaps
    assert first_pair.observed_union_segment_ids == (
        "segment-000000",
        "segment-000001",
    )
    assert first_pair.observed_xor_segment_ids == ("segment-000000",)
    assert second_pair.observed_union_segment_ids == (
        "segment-000001",
        "segment-000002",
        "segment-000003",
    )
    assert second_pair.observed_xor_segment_ids == (
        "segment-000001",
        "segment-000003",
    )


def test_conflict_on_an_unresolved_segment_is_typed_as_deferred() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs = tuple(
        _evidence_job(
            index,
            plan=plan,
            segments=segments,
            block_index=index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                ("segment-000000" if index == 0 else "segment-000002"): (
                    "edge",
                    0.9,
                ),
                "segment-000001": ("first" if index == 0 else "second", 0.9),
            },
        )
        for index in (0, 1)
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    overlap = result.overlaps[0]
    assert overlap.deferred_intersection_segment_ids == ("segment-000001",)
    assert overlap.conflicting_intersection_segment_ids == ()
    assert overlap.missing_intersection_segment_ids == ()
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_overlap_categories_remain_disjoint_and_canonical() -> None:
    fields = {
        "first_block_id": "block-000000",
        "second_block_id": "block-000001",
        "intersection_segment_ids": ("segment-000000", "segment-000001"),
        "union_segment_ids": ("segment-000000", "segment-000001"),
        "xor_segment_ids": (),
        "confirmed_intersection_segment_ids": (
            "segment-000000",
            "segment-000001",
        ),
        "cross_transform_confirmed_intersection_segment_ids": (),
        "near_confirmed_intersection_segment_ids": (),
        "deferred_intersection_segment_ids": (),
        "conflicting_intersection_segment_ids": (),
        "missing_intersection_segment_ids": (),
        "observed_union_segment_ids": ("segment-000000", "segment-000001"),
        "observed_xor_segment_ids": (),
    }
    assert OverlapConsensus(**fields).confirmed_intersection_segment_ids == (
        "segment-000000",
        "segment-000001",
    )

    with pytest.raises(ValueError, match="canonical"):
        OverlapConsensus(
            **{
                **fields,
                "confirmed_intersection_segment_ids": (
                    "segment-000001",
                    "segment-000000",
                ),
            }
        )
    with pytest.raises(ValueError, match="disjoint"):
        OverlapConsensus(
            **{
                **fields,
                "confirmed_intersection_segment_ids": ("segment-000000",),
                "missing_intersection_segment_ids": (
                    "segment-000000",
                    "segment-000001",
                ),
            }
        )


def test_exact_selected_text_across_two_transforms_confirms_overlap() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000000": ("left", 0.9),
                target: ("РАЗДЕЛ A", 0.80),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            evidence={
                "segment-000000": ("left", 0.9),
                target: ("РАЗДЕЛ А", 0.91),
            },
        ),
        _evidence_job(
            2,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                target: ("РАЗДЕЛ А", 0.92),
                "segment-000002": ("right", 0.9),
            },
        ),
        _evidence_job(
            3,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            evidence={
                target: ("РАЗДЕЛ А", 0.91),
                "segment-000002": ("right", 0.9),
            },
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "РАЗДЕЛ А"
    assert fused.selected_transform is OcrTransform.RAW
    assert fused.unresolved is False
    assert overlap.confirmed_intersection_segment_ids == ()
    assert overlap.cross_transform_confirmed_intersection_segment_ids == (target,)
    assert overlap.near_confirmed_intersection_segment_ids == ()
    assert overlap.deferred_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == ()
    assert result.status is OcrFusionStatus.COMPLETE


def test_cross_transform_consensus_requires_independent_context_digests() -> None:
    plan, segments, crops = _overlap_fixture(identical_blocks=True)
    target = "segment-000001"
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={target: ("РАЗДЕЛ A", 0.8)},
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            evidence={target: ("РАЗДЕЛ А", 0.9)},
        ),
        _evidence_job(
            2,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={target: ("РАЗДЕЛ А", 0.95)},
        ),
        _evidence_job(
            3,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            evidence={target: ("РАЗДЕЛ А", 0.9)},
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    overlap = result.overlaps[0]

    assert overlap.cross_transform_confirmed_intersection_segment_ids == ()
    assert overlap.missing_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_low_confidence_cross_transform_witness_cannot_clear_raw_conflict() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000000": ("edge", 0.9),
                target: ("abcd", 0.9),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                target: ("abce", 0.9),
                "segment-000002": ("edge", 0.9),
            },
        ),
        _evidence_job(
            2,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            evidence={
                target: ("abcd", 0.01),
                "segment-000002": ("edge", 0.9),
            },
        ),
    )
    assert jobs[1].output is not None
    shifted_words = tuple(
        replace(word, bbox=Box(1, 0, 99, 10)) if word.text == "abce" else word for word in jobs[1].output.words
    )
    jobs = (
        jobs[0],
        replace(jobs[1], output=replace(jobs[1].output, words=shifted_words)),
        jobs[2],
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "abcd"
    assert fused.unresolved is False
    assert overlap.cross_transform_confirmed_intersection_segment_ids == ()
    assert overlap.near_confirmed_intersection_segment_ids == ()
    assert overlap.deferred_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_exact_overlap_consensus_never_combines_capabilities() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    specified: list[OcrJobResult] = []
    for block_index, edge_segment in (
        (0, "segment-000000"),
        (1, "segment-000002"),
    ):
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
            for lane_id, capability_id, target_text, confidence in (
                (
                    "lane-a",
                    "capability-a",
                    "abcdefghij" if block_index == 0 else "abcdefghil",
                    0.95 if block_index == 0 else 0.90,
                ),
                (
                    "lane-b",
                    "capability-b",
                    "abcdefghik" if block_index == 0 else "abcdefghij",
                    0.90 if block_index == 0 else 0.96,
                ),
            ):
                job = _evidence_job(
                    len(specified),
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=transform,
                    lane_id=lane_id,
                    evidence={
                        edge_segment: ("edge", 0.9),
                        target: (target_text, confidence),
                    },
                )
                specified.append(replace(job, capability_id=capability_id))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *specified),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "abcdefghij"
    assert fused.selected_observation_id is not None
    selected = next(item for item in result.observations if item.observation_id == fused.selected_observation_id)
    assert selected.capability_id == "capability-b"
    assert selected.block_id == "block-000001"
    assert fused.unresolved is False
    assert overlap.cross_transform_confirmed_intersection_segment_ids == ()
    assert overlap.near_confirmed_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_resolved_core_raw_near_consensus_with_two_high_confidence_witnesses() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = tuple(
        _evidence_job(
            block_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                ("segment-000000" if block_index == 0 else "segment-000002"): (
                    "edge",
                    0.9,
                ),
                target: (
                    "abcd" if block_index == 0 else "abce",
                    0.9 if block_index == 0 else 0.8,
                ),
            },
        )
        for block_index in (0, 1)
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "abcd"
    assert fused.stability == pytest.approx(0.75)
    assert fused.unresolved is False
    assert overlap.confirmed_intersection_segment_ids == ()
    assert overlap.cross_transform_confirmed_intersection_segment_ids == ()
    assert overlap.near_confirmed_intersection_segment_ids == (target,)
    assert overlap.deferred_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == ()
    assert result.status is OcrFusionStatus.COMPLETE


def test_low_confidence_overlap_witness_cannot_clear_resolved_core_conflict() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = tuple(
        _evidence_job(
            block_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                ("segment-000000" if block_index == 0 else "segment-000002"): (
                    "edge",
                    0.9,
                ),
                target: (
                    "abcd" if block_index == 0 else "abce",
                    0.9 if block_index == 0 else 0.01,
                ),
            },
        )
        for block_index in (0, 1)
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "abcd"
    assert fused.unresolved is False
    assert overlap.near_confirmed_intersection_segment_ids == ()
    assert overlap.deferred_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_deferred_overlap_reactivates_when_its_segment_becomes_resolved() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    first = _evidence_job(
        0,
        plan=plan,
        segments=segments,
        block_index=0,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        evidence={
            "segment-000000": ("left", 0.9),
            target: ("abcd", 0.9),
        },
    )
    second = _evidence_job(
        1,
        plan=plan,
        segments=segments,
        block_index=1,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        evidence={
            target: ("abce", 0.8),
            "segment-000002": ("right", 0.9),
        },
    )
    assert second.output is not None
    shifted_words = tuple(
        replace(word, bbox=Box(1, 0, 99, 10)) if word.text == "abce" else word for word in second.output.words
    )
    second = replace(
        second,
        output=replace(second.output, words=shifted_words),
    )
    queue = _matrix_queue(plan, crops, first, second)

    unresolved = OcrEvidenceFusion(replace(OcrFusionConfig(), minimum_stability=0.76)).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )
    resolved = OcrEvidenceFusion(replace(OcrFusionConfig(), minimum_stability=0.75)).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )

    assert _fused(unresolved, target).selected_text == "abcd"
    assert _fused(resolved, target).selected_text == "abcd"
    assert _fused(unresolved, target).unresolved is True
    assert unresolved.overlaps[0].deferred_intersection_segment_ids == (target,)
    assert unresolved.overlaps[0].conflicting_intersection_segment_ids == ()
    assert _fused(resolved, target).unresolved is False
    assert resolved.overlaps[0].deferred_intersection_segment_ids == ()
    assert resolved.overlaps[0].near_confirmed_intersection_segment_ids == ()
    assert resolved.overlaps[0].conflicting_intersection_segment_ids == (target,)
    assert unresolved.status is OcrFusionStatus.UNRESOLVED
    assert resolved.status is OcrFusionStatus.UNRESOLVED


def test_low_confidence_disagreement_cannot_be_near_confirmed() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = tuple(
        _evidence_job(
            block_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                ("segment-000000" if block_index == 0 else "segment-000002"): (
                    "edge",
                    0.9,
                ),
                target: (
                    "abcd" if block_index == 0 else "abce",
                    0.4 if block_index == 0 else 0.3,
                ),
            },
        )
        for block_index in (0, 1)
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)
    overlap = result.overlaps[0]

    assert fused.selected_text == "abcd"
    assert "low_confidence" in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert overlap.near_confirmed_intersection_segment_ids == ()
    assert overlap.deferred_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_missing_overlap_is_typed_and_marks_result_unresolved() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                "segment-000000": ("left", 0.9),
                "segment-000001": ("middle", 0.9),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={"segment-000002": ("right", 0.9)},
        ),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    overlap = result.overlaps[0]
    assert overlap.missing_intersection_segment_ids == ("segment-000001",)
    assert overlap.conflicting_intersection_segment_ids == ()
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_observation_segment_overlap_and_alignment_order_is_canonical() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs = (
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu-b",
            evidence={
                "segment-000000": ("zero", 0.8),
                "segment-000001": ("one", 0.8),
            },
        ),
        _evidence_job(
            1,
            plan=plan,
            segments=segments,
            block_index=1,
            transform=OcrTransform.RAW,
            lane_id="cpu-a",
            evidence={
                "segment-000001": ("one", 0.8),
                "segment-000002": ("two", 0.8),
            },
        ),
    )

    first = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    second = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert first == second
    assert tuple(item.segment_id for item in first.segments) == plan.source_segment_ids
    assert tuple((item.observation_id, item.job_id, item.segment_id) for item in first.observations) == (
        ("observation-00000000", "ocr-job-00000000", "segment-000000"),
        ("observation-00000001", "ocr-job-00000000", "segment-000001"),
        ("observation-00000002", "ocr-job-00000005", "segment-000001"),
        ("observation-00000003", "ocr-job-00000005", "segment-000002"),
    )
    middle = _fused(first, "segment-000001")
    assert tuple(item.observation_id for item in middle.alignments) == (
        "observation-00000001",
        "observation-00000002",
    )


def test_geometry_observation_text_and_alignment_limits_fail_closed() -> None:
    plan, segments, crops = _overlap_fixture()
    job = _evidence_job(
        0,
        plan=plan,
        segments=segments,
        block_index=0,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        evidence={"segment-000000": ("four", 0.9)},
    )

    with pytest.raises(OcrFusionLimitError, match="segment count"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_segments=2)).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_matrix_queue(plan, crops, job),
        )
    with pytest.raises(OcrFusionLimitError, match="observations"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_observations=1)).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_matrix_queue(plan, crops, job),
        )
    with pytest.raises(OcrFusionLimitError, match="text"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_text_chars=3)).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_matrix_queue(plan, crops, job),
        )
    with pytest.raises(OcrFusionLimitError, match="cells"):
        align_exact_text("ab", "cd", max_cells=8)


def test_word_to_segment_routing_comparisons_have_an_exact_hard_budget() -> None:
    plan, segments, crops = _three_block_fixture()
    job = _evidence_job(
        0,
        plan=plan,
        segments=segments,
        block_index=0,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        evidence={
            "segment-000000": ("zero", 0.9),
            "segment-000001": ("one", 0.9),
        },
    )
    queue = _matrix_queue(plan, crops, job)

    with pytest.raises(OcrFusionLimitError, match="routing|comparison"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_routing_comparisons=3)).fuse(
            plan=plan, segments=segments, crops=crops, queue=queue
        )

    result = OcrEvidenceFusion(replace(OcrFusionConfig(), max_routing_comparisons=4)).fuse(
        plan=plan, segments=segments, crops=crops, queue=queue
    )
    assert "routing-comparisons=4" in result.diagnostics


def test_production_profile_orthogonal_membership_decodes_all_six() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    texts = {segment.segment_id: (f"value-{index}", 0.95) for index, segment in enumerate(segments)}
    jobs = tuple(
        _evidence_job(
            block_index * 2 + transform_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=transform,
            lane_id="cpu",
            evidence={segment_id: texts[segment_id] for segment_id in plan.blocks[block_index].segment_ids},
        )
        for block_index in range(len(plan.blocks))
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )

    profile = resolve_sparse_runtime_profile()
    assert profile.ocr_fusion.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
    assert profile.ocr_fusion.membership_assume_complete_observations is True
    result = OcrEvidenceFusion(profile.ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.COMPLETE
    assert result.unassigned_word_observations == ()
    assert tuple(item.selected_text for item in result.segments) == tuple(f"value-{index}" for index in range(6))
    observed_blocks = {
        segment_id: {
            observation.block_id
            for observation in result.observations
            if observation.segment_id == segment_id and observation.comparison_text
        }
        for segment_id in plan.source_segment_ids
    }
    assert observed_blocks == {
        segment_id: {block.block_id for block in plan.blocks if segment_id in block.segment_ids}
        for segment_id in plan.source_segment_ids
    }
    assert "routing-mode=block_membership" in result.diagnostics
    assert "or-xor=observed-block-membership-signatures" in result.diagnostics
    assert "membership-completeness=explicit-complete-job-matrix" in result.diagnostics
    assert "membership-per-word-omission-risk=accepted-by-explicit-profile" in result.diagnostics


def test_canonical_polar_slots_prove_placement_omission() -> None:
    plan, segments, _crops_before_locality = _orthogonal_membership_fixture()
    common_bbox = Box(0, 0, *plan.aligned_size)
    local_blocks = []
    for index, block in enumerate(plan.blocks):
        local_block = replace(block, bbox=common_bbox)
        # This unit isolates the fusion boundary.  Planner/renderer tests own
        # construction and validation of the full island metadata payload.
        object.__setattr__(
            local_block,
            "matrix_window_kind",
            ("polar-local-full" if index == 0 else "polar-local-signature"),
        )
        local_blocks.append(local_block)
    plan = replace(
        plan,
        blocks=tuple(local_blocks),
    )
    crops = _crops(plan)
    jobs = tuple(
        _evidence_job(
            block_index * 2 + transform_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=transform,
            lane_id="cpu",
            evidence={
                segment_id: (f"value-{int(segment_id[-6:])}", 0.95)
                for segment_id in plan.blocks[block_index].segment_ids
            },
        )
        for block_index in range(len(plan.blocks))
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )
    queue = _matrix_queue(plan, crops, *jobs)
    profile = replace(
        OcrFusionConfig(),
        routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
        membership_assume_complete_observations=False,
        require_exact_job_matrix=False,
    )

    without_provenance = OcrEvidenceFusion(profile).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )
    with_provenance = OcrEvidenceFusion(profile).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=replace(
            queue,
            diagnostics=("geometry=canonical-membership-slots-v1",),
        ),
    )

    assert without_provenance.status is OcrFusionStatus.UNRESOLVED
    assert with_provenance.unassigned_word_observations == ()
    assert tuple(item.selected_text for item in with_provenance.segments) == tuple(
        f"value-{index}" for index in range(6)
    )
    assert "overlap-contract=canonical-membership-slots" in (with_provenance.diagnostics)


def test_production_membership_does_not_alias_incomplete_separator_signature() -> None:
    """A one-block rule token cannot impersonate a shorter source code.

    Segment 0 has the expected membership signature ``{A, C}``, while segment
    1 has ``{A}``.  OCR observes a spurious table-rule token at segment 0's
    page position in A but omits it in C.  A complete *job* matrix is not proof
    that this individual token has a complete observation signature, so the
    observed ``{A}`` must remain unassigned instead of becoming segment 1.
    """

    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = []
    for block_index, block in enumerate(plan.blocks):
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
            evidence = {
                segment_id: (f"value-{int(segment_id[-6:])}", 0.95)
                for segment_id in block.segment_ids
                if segment_id != "segment-000000"
            }
            if block_index == 0 and transform is OcrTransform.RAW:
                evidence["segment-000000"] = ("|", 0.95)
            jobs.append(
                _evidence_job(
                    block_index * 2 + transform_index,
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=transform,
                    lane_id="cpu",
                    evidence=evidence,
                )
            )

    result = OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.UNRESOLVED
    assert len(result.unassigned_word_observations) == 1
    assert {item.reason for item in result.unassigned_word_observations} == {
        "membership-observation-lattice-incomplete"
    }
    assert all("|" not in (item.selected_text or "") for item in result.segments)
    assert _fused(result, "segment-000001").selected_text == "value-1"


def test_complete_replica_separator_missing_one_spatial_bit_is_unassigned() -> None:
    """Block geometry detects a full-replica short-code alias without glyph rules."""

    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = []
    for block_index, block in enumerate(plan.blocks):
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
            evidence = {
                segment_id: (f"value-{int(segment_id[-6:])}", 0.95)
                for segment_id in block.segment_ids
                if segment_id != "segment-000000"
            }
            if block_index == 0:
                evidence["segment-000000"] = ("|", 0.95)
            jobs.append(
                _evidence_job(
                    block_index * 2 + transform_index,
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=transform,
                    lane_id="cpu",
                    evidence=evidence,
                )
            )

    result = OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.UNRESOLVED
    assert len(result.unassigned_word_observations) == 2
    assert {item.reason for item in result.unassigned_word_observations} == {"membership-signature-omission-ambiguous"}
    assert all("|" not in (item.selected_text or "") for item in result.segments)
    assert _fused(result, "segment-000001").selected_text == "value-1"


def test_complete_separator_lattice_survives_near_geometry_correlation() -> None:
    """A genuine ``|`` is data when every expected observation is present."""

    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = []
    for block_index, block in enumerate(plan.blocks):
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
            job = _evidence_job(
                block_index * 2 + transform_index,
                plan=plan,
                segments=segments,
                block_index=block_index,
                transform=transform,
                lane_id="cpu",
                evidence={
                    segment_id: (
                        "|" if segment_id == "segment-000000" else f"value-{int(segment_id[-6:])}",
                        0.95,
                    )
                    for segment_id in block.segment_ids
                },
            )
            if block_index == 2:
                assert job.output is not None
                shifted = tuple(
                    (
                        replace(
                            word,
                            bbox=Box(
                                word.bbox.left,
                                word.bbox.top + 1,
                                word.bbox.right,
                                word.bbox.bottom + 1,
                            ),
                        )
                        if word.text == "|"
                        else word
                    )
                    for word in job.output.words
                )
                job = replace(job, output=replace(job.output, words=shifted))
            jobs.append(job)

    result = OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.COMPLETE
    assert result.unassigned_word_observations == ()
    assert _fused(result, "segment-000000").selected_text == "|"


def test_one_complete_capability_lattice_is_enough_for_genuine_separator() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = []
    for block_index, block in enumerate(plan.blocks):
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
            for lane_id in ("complete-capability", "partial-capability"):
                evidence = {
                    segment_id: (
                        "|" if segment_id == "segment-000000" else f"value-{int(segment_id[-6:])}",
                        0.95,
                    )
                    for segment_id in block.segment_ids
                    if not (
                        lane_id == "partial-capability"
                        and block_index == 2
                        and transform is OcrTransform.GAMMA
                        and segment_id == "segment-000000"
                    )
                }
                jobs.append(
                    _evidence_job(
                        block_index * 4 + transform_index * 2 + (lane_id == "partial-capability"),
                        plan=plan,
                        segments=segments,
                        block_index=block_index,
                        transform=transform,
                        lane_id=lane_id,
                        evidence=evidence,
                    )
                )

    result = OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.COMPLETE
    assert result.unassigned_word_observations == ()
    assert _fused(result, "segment-000000").selected_text == "|"


def test_near_duplicate_separator_boxes_are_unassigned_as_geometry_garbage() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = []
    for block_index, block in enumerate(plan.blocks):
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA)):
            job = _evidence_job(
                block_index * 2 + transform_index,
                plan=plan,
                segments=segments,
                block_index=block_index,
                transform=transform,
                lane_id="cpu",
                evidence={segment_id: (f"value-{int(segment_id[-6:])}", 0.95) for segment_id in block.segment_ids},
            )
            if block_index == 0 and transform is OcrTransform.RAW:
                assert job.output is not None
                garbage = (
                    OcrWord("|", Box(5, 3, 9, 10), 0.4),
                    OcrWord("|", Box(6, 3, 10, 10), 0.4),
                )
                job = replace(
                    job,
                    output=replace(
                        job.output,
                        text=job.output.text + " | |",
                        words=job.output.words + garbage,
                    ),
                )
            jobs.append(job)

    result = OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.UNRESOLVED
    assert len(result.unassigned_word_observations) == 2
    assert {item.reason for item in result.unassigned_word_observations} == {"membership-geometry-ambiguous"}
    assert all("|" not in (item.selected_text or "") for item in result.segments)


def test_production_membership_profile_rejects_one_failed_block_job() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = tuple(
        _evidence_job(
            block_index * 2 + transform_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=transform,
            lane_id="cpu",
            evidence={segment_id: (segment_id, 0.95) for segment_id in plan.blocks[block_index].segment_ids},
        )
        for block_index in range(len(plan.blocks))
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )
    failed = _failed_job(
        0,
        block=plan.blocks[0],
        transform=OcrTransform.RAW,
        lane_id="cpu",
    )
    queue = _matrix_queue(plan, crops, failed, *jobs[1:])

    with pytest.raises(
        OcrFusionInvariantError,
        match="every RAW/GAMMA block job",
    ):
        OcrEvidenceFusion(resolve_sparse_runtime_profile().ocr_fusion).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
        )


def test_membership_routing_missing_context_is_unmatched_and_fail_closed() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = tuple(
        _evidence_job(
            block_index * 2 + transform_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=transform,
            lane_id="cpu",
            evidence={
                segment_id: (f"value-{int(segment_id[-6:])}", 0.95)
                for segment_id in plan.blocks[block_index].segment_ids
                # Segment 0 is observed in C but missing in A.  Its observed
                # signature is {C}, which is not a source signature.
                if not (block_index == 0 and segment_id == "segment-000000")
            },
        )
        for block_index in range(len(plan.blocks))
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )

    result = OcrEvidenceFusion(
        replace(
            OcrFusionConfig(),
            routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
            membership_assume_complete_observations=True,
        )
    ).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.UNRESOLVED
    assert _fused(result, "segment-000000").selected_text is None
    assert tuple((item.block_id, item.transform, item.reason) for item in result.unassigned_word_observations) == (
        (
            "block-000002",
            OcrTransform.RAW,
            "membership-signature-unmatched",
        ),
        (
            "block-000002",
            OcrTransform.GAMMA,
            "membership-signature-unmatched",
        ),
    )


def test_membership_default_rejects_exact_code_that_could_have_lost_a_bit() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    # Both observed words have {A}.  For segment 1 this is its exact code;
    # for segment 0 it is {A,C} with the C observation missing.  Geometry
    # routing could distinguish their source boxes, but membership routing
    # deliberately cannot and therefore rejects both in omission-safe mode.
    job = _evidence_job(
        0,
        plan=plan,
        segments=segments,
        block_index=0,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        evidence={
            "segment-000000": ("could-be-A-or-AC", 0.95),
            "segment-000001": ("exact-A", 0.95),
        },
    )

    result = OcrEvidenceFusion(
        replace(
            OcrFusionConfig(),
            routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
        )
    ).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, job),
    )

    assert result.status is OcrFusionStatus.UNRESOLVED
    assert len(result.unassigned_word_observations) == 2
    assert {item.reason for item in result.unassigned_word_observations} == {"membership-signature-omission-ambiguous"}
    assert all(item.selected_text is None for item in result.segments)
    assert "membership-completeness=omission-safe" in result.diagnostics


def test_membership_routing_attributes_duplicate_signature_to_subblock() -> None:
    segments = (
        _segment("segment-000000", Box(0, 0, 20, 12), 0),
        _segment("segment-000001", Box(20, 0, 40, 12), 0),
    )
    block = RecognitionBlock(
        "block-000000",
        Box(0, 0, 40, 12),
        tuple(item.segment_id for item in segments),
        tuple(item.segment_id for item in segments),
        (),
        ("object-000000",),
        "scope-000000",
    )
    plan = BlockPlan(
        aligned_size=(40, 12),
        source_segment_ids=tuple(item.segment_id for item in segments),
        blocks=(block,),
        adjacent_algebra=(),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=(
            MembershipUnit(
                "membership-unit-000000",
                MembershipUnitKind.SUBBLOCK,
                tuple(item.segment_id for item in segments),
                ("block-000000",),
                "scope-000000",
            ),
        ),
        matrix_sha256="0" * 64,
    )
    crops = _crops(plan)
    jobs = tuple(
        _evidence_job(
            transform_index,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=transform,
            lane_id="cpu",
            evidence={segment.segment_id: (f"value-{index}", 0.95) for index, segment in enumerate(segments)},
        )
        for transform_index, transform in enumerate((OcrTransform.RAW, OcrTransform.GAMMA))
    )

    result = OcrEvidenceFusion(
        replace(
            OcrFusionConfig(),
            routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
            membership_assume_complete_observations=True,
        )
    ).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )

    assert result.status is OcrFusionStatus.COMPLETE
    assert all(item.selected_text is None for item in result.segments)
    assert result.unassigned_word_observations == ()
    assert len(result.segment_groups) == 1
    assert result.segment_groups[0].segment_ids == tuple(item.segment_id for item in segments)
    assert result.segment_groups[0].selected_text == "value-0 value-1"
    assert result.segment_groups[0].selected_observation_id is not None


def test_membership_geometry_comparisons_have_a_hard_budget() -> None:
    plan, segments, crops = _orthogonal_membership_fixture()
    jobs = tuple(
        _evidence_job(
            block_index,
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={segment_id: (segment_id, 0.9) for segment_id in plan.blocks[block_index].segment_ids},
        )
        for block_index in range(len(plan.blocks))
    )
    queue = _matrix_queue(plan, crops, *jobs)

    with pytest.raises(OcrFusionLimitError, match="membership geometry"):
        OcrEvidenceFusion(
            replace(
                OcrFusionConfig(),
                routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
                membership_assume_complete_observations=False,
                max_membership_geometry_comparisons=1,
            )
        ).fuse(plan=plan, segments=segments, crops=crops, queue=queue)
    with pytest.raises(OcrFusionLimitError, match="membership assignments"):
        OcrEvidenceFusion(
            replace(
                OcrFusionConfig(),
                routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
                membership_assume_complete_observations=False,
                max_membership_block_assignments=10,
            )
        ).fuse(plan=plan, segments=segments, crops=crops, queue=queue)
    with pytest.raises(OcrFusionLimitError, match="signature comparisons"):
        OcrEvidenceFusion(
            replace(
                OcrFusionConfig(),
                routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
                max_membership_signature_comparisons=1,
            )
        ).fuse(plan=plan, segments=segments, crops=crops, queue=queue)


def test_aggregate_alignment_and_pair_limits_are_enforced() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs = tuple(
        _evidence_job(
            index,
            plan=plan,
            segments=segments,
            block_index=index % 2,
            transform=OcrTransform.RAW,
            lane_id=f"cpu-{index}",
            evidence={target: (text, 0.9)},
        )
        for index, text in enumerate(("alpha", "bravo", "charlie"))
    )

    with pytest.raises(OcrFusionLimitError, match="logical comparison"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_pairwise_alignments=1)).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_matrix_queue(plan, crops, *jobs),
        )
    with pytest.raises(OcrFusionLimitError, match="aggregate cell"):
        OcrEvidenceFusion(
            replace(
                OcrFusionConfig(),
                max_alignment_cells=1_000,
                max_total_alignment_cells=36,
            )
        ).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_matrix_queue(plan, crops, *jobs),
        )


def test_fusion_rejects_forged_geometry_and_noncanonical_queue() -> None:
    plan, segments, crops = _overlap_fixture()
    job = _complete_job(
        0,
        block=plan.blocks[0],
        transform=OcrTransform.RAW,
        lane_id="cpu",
    )
    valid_queue = _matrix_queue(plan, crops, job)
    forged_jobs = list(valid_queue.jobs)
    forged_jobs[0] = replace(forged_jobs[0], block_id="block-forged")
    with pytest.raises(OcrFusionInvariantError, match="matrix"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(*forged_jobs),
        )
    noncanonical_jobs = list(valid_queue.jobs)
    noncanonical_jobs[0] = replace(
        noncanonical_jobs[0],
        job_id="ocr-job-00000009",
    )
    with pytest.raises(OcrFusionInvariantError, match="canonical"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(*noncanonical_jobs),
        )
    with pytest.raises(OcrFusionInvariantError, match="segments"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=tuple(reversed(segments[:-1])),
            crops=crops,
            queue=valid_queue,
        )


def test_one_thousand_identical_observations_do_not_expand_quadratically() -> None:
    plan, segments, crops = _single_segment_fixture()
    segment = segments[0]
    block = plan.blocks[0]
    specified: list[OcrJobResult] = []
    for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
        for lane_index in range(500):
            specified.append(
                _complete_job(
                    len(specified),
                    block=block,
                    transform=transform,
                    lane_id=f"lane-{lane_index:04d}",
                    words=(_word(segment, block, "identical", 0.9),),
                )
            )

    result = OcrEvidenceFusion(replace(OcrFusionConfig(), max_pairwise_alignments=5_000)).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *specified),
    )

    fused = result.segments[0]
    assert fused.observation_count == 1_000
    assert fused.selected_text == "identical"
    logical = next(
        int(item.rsplit("=", 1)[1]) for item in result.diagnostics if item.startswith("alignment-logical-comparisons=")
    )
    assert logical <= 2_005


def test_reading_order_quadratic_work_has_an_exact_aggregate_boundary() -> None:
    plan, segments, crops = _single_segment_fixture()
    block = plan.blocks[0]
    words = (
        OcrWord("one", Box(0, 0, 8, 12), 0.9),
        OcrWord("two", Box(8, 0, 16, 12), 0.9),
        OcrWord("three", Box(16, 0, 24, 12), 0.9),
    )
    queue = _matrix_queue(
        plan,
        crops,
        _complete_job(
            0,
            block=block,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            words=words,
        ),
        _complete_job(
            1,
            block=block,
            transform=OcrTransform.GAMMA,
            lane_id="cpu",
            words=words,
        ),
    )

    with pytest.raises(OcrFusionLimitError, match="reading-order"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_reading_order_checks=9)).fuse(
            plan=plan, segments=segments, crops=crops, queue=queue
        )

    result = OcrEvidenceFusion(replace(OcrFusionConfig(), max_reading_order_checks=10)).fuse(
        plan=plan, segments=segments, crops=crops, queue=queue
    )
    assert result.segments[0].selected_text == "one two three"
    assert "reading-order-checks=10" in result.diagnostics


def test_job_matrix_product_is_preflighted_before_expected_tuple_materialization() -> None:
    plan, segments, crops = _overlap_fixture()
    first_job = _matrix_queue(
        plan,
        crops,
        _evidence_job(
            0,
            plan=plan,
            segments=segments,
            block_index=0,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={"segment-000000": ("one", 0.9)},
        ),
    ).jobs[0]
    # One discovered lane over two blocks requires 2*2*1=4 jobs.  The supplied
    # queue remains tiny; fusion must reject the arithmetic product before it
    # builds an expected matrix tuple.
    with pytest.raises(OcrFusionLimitError, match="block-transform-lane"):
        OcrEvidenceFusion(replace(OcrFusionConfig(), max_jobs=3)).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(first_job),
        )


def test_identical_crop_fingerprint_under_two_block_ids_is_one_context() -> None:
    plan, segments, crops = _overlap_fixture(identical_blocks=True)
    assert hashlib.sha256(crops[0].raw.png_bytes).digest() == hashlib.sha256(crops[1].raw.png_bytes).digest()
    target = "segment-000001"
    jobs: list[OcrJobResult] = []
    for transform, text in (
        (OcrTransform.RAW, "raw-a"),
        (OcrTransform.GAMMA, "gamma-b"),
    ):
        for block_index in (0, 1):
            jobs.append(
                _evidence_job(
                    len(jobs),
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=transform,
                    lane_id="cpu",
                    evidence={target: (text, 0.95)},
                )
            )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert fused.independent_context_count == 1
    assert fused.selected_text == "raw-a"
    assert fused.selected_transform is OcrTransform.RAW
    assert "transform_conflict" not in fused.uncertainty_reasons
    overlap = result.overlaps[0]
    assert overlap.confirmed_intersection_segment_ids == ()
    assert overlap.conflicting_intersection_segment_ids == ()
    assert overlap.missing_intersection_segment_ids == (target,)
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_stable_raw_and_disagreeing_stable_gamma_retains_raw_as_unresolved() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs: list[OcrJobResult] = []
    for transform, text in (
        (OcrTransform.RAW, "raw-a"),
        (OcrTransform.GAMMA, "gamma-b"),
    ):
        for block_index in (0, 1):
            jobs.append(
                _evidence_job(
                    len(jobs),
                    plan=plan,
                    segments=segments,
                    block_index=block_index,
                    transform=transform,
                    lane_id="cpu",
                    evidence={target: (text, 0.9)},
                )
            )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert fused.selected_text == "raw-a"
    assert fused.selected_transform is OcrTransform.RAW
    assert "transform_conflict" in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_low_confidence_single_block_evidence_is_typed_unresolved() -> None:
    plan, segments, crops = _single_segment_fixture()
    block = plan.blocks[0]
    raw = _complete_job(
        0,
        block=block,
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(_word(segments[0], block, "weak", 0.4),),
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, raw),
    )
    fused = result.segments[0]

    assert fused.selected_text == "weak"
    assert set(fused.uncertainty_reasons) >= {"single_context", "low_confidence"}
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_low_confidence_replicas_of_one_context_do_not_form_consensus() -> None:
    plan, segments, crops = _single_segment_fixture()
    block = plan.blocks[0]
    jobs = tuple(
        replace(
            _complete_job(
                index,
                block=block,
                transform=OcrTransform.RAW,
                lane_id=f"replica-{index}",
                words=(_word(segments[0], block, "same", 0.4),),
            ),
            capability_id="cap-shared",
        )
        for index in range(2)
    )

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = result.segments[0]

    assert fused.observation_count == 2
    assert fused.independent_context_count == 1
    assert "low_confidence" in fused.uncertainty_reasons
    assert "low_confidence_stable_context_consensus" not in fused.uncertainty_reasons
    assert fused.unresolved is True


def test_low_confidence_exact_raw_text_in_two_true_contexts_is_nonblocking() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs: list[OcrJobResult] = []
    for block_index, edge_segment_id, edge_text in (
        (0, "segment-000000", "left"),
        (1, "segment-000002", "right"),
    ):
        job = _evidence_job(
            len(jobs),
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                edge_segment_id: (edge_text, 0.9),
                "segment-000001": ("stable weak text", 0.4),
            },
        )
        jobs.append(replace(job, capability_id="cap-shared"))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, "segment-000001")

    assert fused.selected_text == "stable weak text"
    assert fused.confidence == pytest.approx(0.4)
    assert fused.independent_context_count == 2
    assert fused.stability == pytest.approx(1.0)
    assert fused.uncertainty_reasons == ("low_confidence_stable_context_consensus",)
    assert fused.unresolved is False
    assert result.status is OcrFusionStatus.COMPLETE


def test_low_confidence_similar_but_nonexact_contexts_remain_unresolved() -> None:
    plan, segments, crops = _overlap_fixture()
    jobs: list[OcrJobResult] = []
    for block_index, target_text in ((0, "context"), (1, "contexts")):
        edge_segment_id = f"segment-{block_index * 2:06d}"
        job = _evidence_job(
            len(jobs),
            plan=plan,
            segments=segments,
            block_index=block_index,
            transform=OcrTransform.RAW,
            lane_id="cpu",
            evidence={
                edge_segment_id: ("edge", 0.9),
                "segment-000001": (target_text, 0.4),
            },
        )
        jobs.append(replace(job, capability_id="cap-shared"))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, "segment-000001")

    assert fused.stability >= OcrFusionConfig().minimum_stability
    assert "unstable_raw_text" not in fused.uncertainty_reasons
    assert "low_confidence" in fused.uncertainty_reasons
    assert "low_confidence_stable_context_consensus" not in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_low_confidence_context_consensus_does_not_hide_transform_conflict() -> None:
    plan, segments, crops = _overlap_fixture()
    target = "segment-000001"
    jobs: list[OcrJobResult] = []
    for transform, text in (
        (OcrTransform.RAW, "raw weak"),
        (OcrTransform.GAMMA, "gamma weak"),
    ):
        for block_index in (0, 1):
            job = _evidence_job(
                len(jobs),
                plan=plan,
                segments=segments,
                block_index=block_index,
                transform=transform,
                lane_id="cpu",
                evidence={target: (text, 0.4)},
            )
            jobs.append(replace(job, capability_id="cap-shared"))

    result = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_matrix_queue(plan, crops, *jobs),
    )
    fused = _fused(result, target)

    assert "low_confidence_stable_context_consensus" in fused.uncertainty_reasons
    assert "low_confidence" not in fused.uncertainty_reasons
    assert "transform_conflict" in fused.uncertainty_reasons
    assert fused.unresolved is True
    assert result.status is OcrFusionStatus.UNRESOLVED


def test_four_of_five_exact_contexts_ignore_one_low_confidence_outlier() -> None:
    fused = _fuse_raw_context_votes(
        (
            ("trusted exact consensus", 0.934),
            ("trusted exact consensus", 0.931),
            ("trusted exact consensus", 0.929),
            ("trusted exact consensus", 0.927),
            ("unrelated noisy outlier", 0.416),
        )
    )

    assert fused.selected_text == "trusted exact consensus"
    assert fused.independent_context_count == 5
    assert fused.stability < OcrFusionConfig().minimum_stability
    assert "unstable_raw_text" not in fused.uncertainty_reasons
    assert fused.unresolved is False


@pytest.mark.parametrize(
    "votes",
    (
        (
            ("trusted exact consensus", 0.934),
            ("trusted exact consensus", 0.931),
            ("trusted exact consensus", 0.929),
            ("minority noisy text", 0.416),
            ("minority noisy text", 0.412),
        ),
        (
            ("trusted exact consensus", 0.934),
            ("trusted exact consensus", 0.931),
            ("trusted exact consensus", 0.929),
            ("trusted exact consensus", 0.927),
            ("high confidence conflict", 0.92),
        ),
    ),
    ids=("three-of-five", "high-confidence-conflict"),
)
def test_exact_context_majority_does_not_bypass_ambiguous_votes(
    votes: tuple[tuple[str, float], ...],
) -> None:
    fused = _fuse_raw_context_votes(votes)

    assert fused.stability < OcrFusionConfig().minimum_stability
    assert "unstable_raw_text" in fused.uncertainty_reasons
    assert fused.unresolved is True


def test_exact_job_matrix_and_crop_hashes_are_required() -> None:
    plan, segments, crops = _single_segment_fixture()
    raw = _complete_job(
        0,
        block=plan.blocks[0],
        transform=OcrTransform.RAW,
        lane_id="cpu",
        words=(_word(segments[0], plan.blocks[0], "bound", 0.9),),
    )
    valid = _matrix_queue(plan, crops, raw)

    missing = _queue(*valid.jobs[:-1])
    with pytest.raises(OcrFusionInvariantError, match="matrix"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=missing,
        )
    sparse = OcrEvidenceFusion(OcrFusionConfig(require_exact_job_matrix=False)).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=_queue(valid.jobs[0]),
    )
    assert sparse.observations
    sparse_gamma = OcrEvidenceFusion(OcrFusionConfig(require_exact_job_matrix=False)).fuse(
        plan=plan,
        segments=segments,
        crops=(replace(crops[0], gamma=None),),
        queue=_queue(replace(valid.jobs[1], job_id="ocr-job-00000000")),
    )
    assert sparse_gamma.source_segment_ids == plan.source_segment_ids

    forged_hash_jobs = list(valid.jobs)
    forged_hash_jobs[0] = replace(
        forged_hash_jobs[0],
        input_sha256="0" * 64,
    )
    with pytest.raises(OcrFusionInvariantError, match="bound|crop"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(*forged_hash_jobs),
        )

    forged_context_jobs = list(valid.jobs)
    forged_context_jobs[1] = replace(
        forged_context_jobs[1],
        context_sha256="f" * 64,
    )
    with pytest.raises(OcrFusionInvariantError, match="bound|crop"):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(*forged_context_jobs),
        )


def test_config_rejects_invalid_limits_and_thresholds() -> None:
    for field in (
        "max_segments",
        "max_jobs",
        "max_lanes",
        "max_observations",
        "max_text_chars",
        "max_alignment_cells",
        "max_total_alignment_cells",
        "max_pairwise_alignments",
        "max_routing_comparisons",
        "max_membership_geometry_comparisons",
        "max_membership_sweep_checks",
        "max_reading_order_checks",
        "max_membership_block_assignments",
        "max_membership_signature_comparisons",
        "minimum_alternative_contexts",
    ):
        with pytest.raises(ValueError, match="positive"):
            replace(OcrFusionConfig(), **{field: 0})
        with pytest.raises(ValueError, match="positive"):
            replace(OcrFusionConfig(), **{field: True})
    for field, value in (
        ("minimum_stability", -0.1),
        ("minimum_stability", math.nan),
        ("minimum_confidence", 1.1),
        ("minimum_confidence", True),
        ("minimum_membership_bbox_iou", -0.1),
        ("maximum_membership_center_distance_fraction", 1.1),
    ):
        with pytest.raises(ValueError, match="between zero and one"):
            replace(OcrFusionConfig(), **{field: value})
    with pytest.raises(ValueError, match="routing_mode"):
        replace(OcrFusionConfig(), routing_mode="block_membership")
    with pytest.raises(ValueError, match="boolean"):
        replace(
            OcrFusionConfig(),
            membership_assume_complete_observations=1,
        )


def test_public_fuse_api_has_no_expected_or_reference_channel() -> None:
    parameters = inspect.signature(OcrEvidenceFusion.fuse).parameters
    assert tuple(parameters) == ("self", "plan", "segments", "crops", "queue")
    assert "expected" not in parameters
    assert "reference" not in parameters

    plan, segments, crops = _overlap_fixture()
    with pytest.raises(TypeError):
        OcrEvidenceFusion().fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=_queue(),
            expected="forbidden oracle text",  # type: ignore[call-arg]
        )
