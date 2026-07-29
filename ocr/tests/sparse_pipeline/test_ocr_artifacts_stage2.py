from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningMode,
    BlockSetAlgebra,
    MembershipUnit,
    MembershipUnitKind,
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
    CropInput,
    EnhancementBackend,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrAttributionStatus,
    OcrFailureCode,
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_artifacts import OcrArtifactWriter
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionResult,
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
from tests.sparse_pipeline.test_ocr_fusion_stage2 import (
    _evidence_job as _fusion_evidence_job,
    _matrix_queue as _fusion_matrix_queue,
    _overlap_fixture as _fusion_overlap_fixture,
)


def _page_png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (20, 30), "white")
    try:
        image.save(output, format="PNG", dpi=(300, 300), optimize=False)
    finally:
        image.close()
    return output.getvalue()


def _plan() -> BlockPlan:
    return BlockPlan(
        aligned_size=(20, 30),
        source_segment_ids=("segment-000000", "segment-000001", "segment-000002"),
        blocks=(
            RecognitionBlock(
                block_id="block-000000",
                bbox=Box(0, 0, 20, 20),
                core_segment_ids=("segment-000000", "segment-000001"),
                segment_ids=("segment-000000", "segment-000001"),
                context_segment_ids=(),
                object_ids=("object-000000",),
            ),
            RecognitionBlock(
                block_id="block-000001",
                bbox=Box(0, 10, 20, 30),
                core_segment_ids=("segment-000002",),
                segment_ids=("segment-000001", "segment-000002"),
                context_segment_ids=("segment-000001",),
                object_ids=("object-000001",),
            ),
        ),
        adjacent_algebra=(
            BlockSetAlgebra(
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
            ),
        ),
    )


def _crops(plan: BlockPlan) -> tuple[BlockCropPair, ...]:
    return BlockCropper(
        BlockCropConfig(enhancement_backend=EnhancementBackend.NUMPY)
    ).crop(
        CropInput("artifact-page", _page_png()),
        aligned_size=plan.aligned_size,
        plan=plan,
    )


def _complete_job(
    *,
    job_id: str,
    crop: BlockCropPair,
    transform: OcrTransform,
    output: OcrEngineOutput,
    lane_id: str = "lane-a",
    capability_id: str = "capability-a",
) -> OcrJobResult:
    payload = (
        crop.raw.png_bytes
        if transform is OcrTransform.RAW
        else crop.gamma.png_bytes
    )
    return OcrJobResult(
        job_id=job_id,
        block_id=crop.block_id,
        transform=transform,
        lane_id=lane_id,
        resource=OcrResource.CPU,
        status=OcrJobStatus.COMPLETE,
        output=output,
        error_type=None,
        error_message=None,
        elapsed_seconds=0.125,
        input_sha256=hashlib.sha256(payload).hexdigest(),
        context_sha256=hashlib.sha256(crop.raw.png_bytes).hexdigest(),
        capability_id=capability_id,
    )


def _evidence() -> tuple[
    BlockPlan,
    tuple[Segment, ...],
    tuple[BlockCropPair, ...],
    OcrQueueResult,
    OcrFusionResult,
]:
    plan = _plan()
    segments = (
        Segment(
            segment_id="segment-000000",
            bbox=Box(1, 1, 6, 5),
            source_bbox=Box(1, 1, 6, 5),
            kind=SegmentKind.TEXT,
            ink_pixels=20,
            row_index=0,
            order_key=(0, 0),
            parent_path=("root",),
        ),
        Segment(
            segment_id="segment-000001",
            bbox=Box(1, 12, 6, 16),
            source_bbox=Box(1, 12, 6, 16),
            kind=SegmentKind.TEXT,
            ink_pixels=20,
            row_index=1,
            order_key=(1, 0),
            parent_path=("root",),
        ),
        Segment(
            segment_id="segment-000002",
            bbox=Box(1, 24, 6, 28),
            source_bbox=Box(1, 24, 6, 28),
            kind=SegmentKind.TEXT,
            ink_pixels=20,
            row_index=2,
            order_key=(2, 0),
            parent_path=("root",),
        ),
    )
    crops = _crops(plan)
    jobs: list[OcrJobResult] = []
    lane_ids = ("lane-a", "lane-b")
    for block_index, crop in enumerate(crops):
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
            for lane_id in lane_ids:
                job_id = f"ocr-job-{len(jobs):08d}"
                if (
                    block_index == 1
                    and transform is OcrTransform.GAMMA
                    and lane_id == "lane-b"
                ):
                    payload = crop.gamma.png_bytes
                    jobs.append(
                        OcrJobResult(
                            job_id=job_id,
                            block_id=crop.block_id,
                            transform=transform,
                            lane_id=lane_id,
                            resource=OcrResource.CPU,
                            status=OcrJobStatus.FAILED,
                            output=None,
                            error_type="FixtureFailure",
                            error_message="fixture failed safely",
                            elapsed_seconds=0.25,
                            input_sha256=hashlib.sha256(payload).hexdigest(),
                            context_sha256=hashlib.sha256(
                                crop.raw.png_bytes
                            ).hexdigest(),
                            failure_code=OcrFailureCode.ENGINE_ERROR,
                            capability_id="capability-shared",
                        )
                    )
                    continue
                if block_index == 0 and transform is OcrTransform.RAW:
                    first_text = "Alpha" if lane_id == "lane-a" else "Alphx"
                    output = OcrEngineOutput(
                        f"{first_text} Outside",
                        (
                            OcrWord(first_text, Box(1, 1, 6, 5), 0.91),
                            OcrWord("Outside", Box(15, 6, 19, 9), 0.3),
                        ),
                        OcrOutputGeometry.WORD_BOXES,
                    )
                elif block_index == 0:
                    output = OcrEngineOutput(
                        "Unattributable block text",
                        (),
                        OcrOutputGeometry.TEXT_ONLY,
                    )
                else:
                    output = OcrEngineOutput(
                        "Beta Gamma",
                        (
                            OcrWord("Beta", Box(1, 2, 6, 6), 0.82),
                            OcrWord("Gamma", Box(1, 14, 6, 18), 0.88),
                        ),
                        OcrOutputGeometry.WORD_BOXES,
                    )
                jobs.append(
                    _complete_job(
                        job_id=job_id,
                        crop=crop,
                        transform=transform,
                        output=output,
                        lane_id=lane_id,
                        capability_id="capability-shared",
                    )
                )
    queue = OcrQueueResult(
        jobs=tuple(jobs),
        status=OcrQueueStatus.PARTIAL,
        complete=7,
        failed=1,
        diagnostics=("fixture-queue-diagnostic",),
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )
    assert fusion.block_text_observations
    assert fusion.unassigned_word_observations
    assert fusion.replica_conflicts
    assert fusion.overlaps
    return plan, segments, crops, queue, fusion


def _jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


_OVERLAP_CATEGORY_FIELDS = (
    "confirmed_intersection_segment_ids",
    "cross_transform_confirmed_intersection_segment_ids",
    "near_confirmed_intersection_segment_ids",
    "deferred_intersection_segment_ids",
    "conflicting_intersection_segment_ids",
    "missing_intersection_segment_ids",
)


def _typed_overlap_evidence(
    category: str,
) -> tuple[
    BlockPlan,
    tuple[Segment, ...],
    tuple[BlockCropPair, ...],
    OcrQueueResult,
    OcrFusionResult,
]:
    plan, segments, crops = _fusion_overlap_fixture()
    target = "segment-000001"
    if category == "cross_transform":
        jobs = (
            _fusion_evidence_job(
                0,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu",
                evidence={
                    "segment-000000": ("left", 0.9),
                    target: ("РАЗДЕЛ A", 0.8),
                },
            ),
            _fusion_evidence_job(
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
            _fusion_evidence_job(
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
            _fusion_evidence_job(
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
    else:
        first_text, first_confidence, second_text, second_confidence = {
            "confirmed": ("same", 0.9, "same", 0.9),
            "near": ("abcd", 0.9, "abce", 0.8),
            "deferred": ("abcd", 0.4, "abce", 0.3),
            "conflicting": ("abcd", 0.9, "abce", 0.8),
            "missing": ("present", 0.9, None, None),
        }[category]
        second_evidence: dict[str, tuple[str, float]] = {
            "segment-000002": ("right", 0.9),
        }
        if second_text is not None and second_confidence is not None:
            second_evidence[target] = (second_text, second_confidence)
        jobs = (
            _fusion_evidence_job(
                0,
                plan=plan,
                segments=segments,
                block_index=0,
                transform=OcrTransform.RAW,
                lane_id="cpu",
                evidence={
                    "segment-000000": ("left", 0.9),
                    target: (first_text, first_confidence),
                },
            ),
            _fusion_evidence_job(
                1,
                plan=plan,
                segments=segments,
                block_index=1,
                transform=OcrTransform.RAW,
                lane_id="cpu",
                evidence=second_evidence,
            ),
        )
        if category == "conflicting":
            assert jobs[1].output is not None
            shifted_words = tuple(
                replace(word, bbox=Box(1, 0, 99, 10))
                if word.text == "abce"
                else word
                for word in jobs[1].output.words
            )
            jobs = (
                jobs[0],
                replace(
                    jobs[1],
                    output=replace(jobs[1].output, words=shifted_words),
                ),
            )

    queue = _fusion_matrix_queue(plan, crops, *jobs)
    fusion = OcrEvidenceFusion(OcrFusionConfig()).fuse(
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
    )
    return plan, segments, crops, queue, fusion


@pytest.mark.parametrize(
    ("category", "field", "manifest_key"),
    (
        ("confirmed", _OVERLAP_CATEGORY_FIELDS[0], "overlap_exact_confirmed"),
        (
            "cross_transform",
            _OVERLAP_CATEGORY_FIELDS[1],
            "overlap_cross_transform_confirmed",
        ),
        ("near", _OVERLAP_CATEGORY_FIELDS[2], "overlap_near_confirmed"),
        ("deferred", _OVERLAP_CATEGORY_FIELDS[3], "overlap_deferred"),
        ("conflicting", _OVERLAP_CATEGORY_FIELDS[4], "overlap_conflicts"),
        ("missing", _OVERLAP_CATEGORY_FIELDS[5], "overlap_missing"),
    ),
)
def test_writer_roundtrips_each_nonempty_overlap_category_as_exact_json_bytes(
    tmp_path: Path,
    category: str,
    field: str,
    manifest_key: str,
) -> None:
    plan, segments, crops, queue, fusion = _typed_overlap_evidence(category)
    overlap = fusion.overlaps[0]
    assert getattr(overlap, field) == ("segment-000001",)
    assert all(
        not getattr(overlap, other)
        for other in _OVERLAP_CATEGORY_FIELDS
        if other != field
    )
    destination = OcrArtifactWriter().write(
        tmp_path,
        run_id=f"typed-{category}",
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
        fusion=fusion,
    )
    stage = destination / "02-ocr"
    expected_record = {
        "first_block_id": overlap.first_block_id,
        "second_block_id": overlap.second_block_id,
        "intersection_segment_ids": list(overlap.intersection_segment_ids),
        "union_segment_ids": list(overlap.union_segment_ids),
        "xor_segment_ids": list(overlap.xor_segment_ids),
        **{
            name: list(getattr(overlap, name))
            for name in _OVERLAP_CATEGORY_FIELDS
        },
        "observed_union_segment_ids": list(overlap.observed_union_segment_ids),
        "observed_xor_segment_ids": list(overlap.observed_xor_segment_ids),
    }
    expected_bytes = (
        json.dumps(expected_record, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    actual_bytes = (stage / "overlaps.jsonl").read_bytes()
    assert actual_bytes == expected_bytes
    assert json.loads(actual_bytes) == expected_record

    manifest = json.loads((stage / "manifest.json").read_bytes())
    assert manifest[manifest_key] == 1


def test_writer_publishes_every_stage2_evidence_category_exactly(
    tmp_path: Path,
) -> None:
    plan, segments, crops, queue, fusion = _evidence()
    destination = OcrArtifactWriter().write(
        tmp_path,
        run_id="complete-evidence",
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
        fusion=fusion,
    )
    stage = destination / "02-ocr"

    segment_inputs = _jsonl(stage / "inputs" / "segments.jsonl")
    assert tuple(record["segment_id"] for record in segment_inputs) == (
        plan.source_segment_ids
    )
    assert tuple(tuple(record["bbox"]) for record in segment_inputs) == tuple(
        segment.bbox.as_tuple() for segment in segments
    )
    assert tuple(
        tuple(record["source_bbox"]) for record in segment_inputs
    ) == tuple(segment.source_bbox.as_tuple() for segment in segments)
    plan_record = json.loads(
        (stage / "inputs" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan_record["aligned_size"] == list(plan.aligned_size)
    assert plan_record["source_segment_ids"] == list(plan.source_segment_ids)
    assert [record["block_id"] for record in plan_record["blocks"]] == [
        block.block_id for block in plan.blocks
    ]
    assert [record["bbox"] for record in plan_record["blocks"]] == [
        list(block.bbox.as_tuple()) for block in plan.blocks
    ]
    assert plan_record["adjacent_algebra"] == [
        {
            "first_block_id": item.first_block_id,
            "second_block_id": item.second_block_id,
            "intersection_segment_ids": list(item.intersection_segment_ids),
            "union_segment_ids": list(item.union_segment_ids),
            "xor_segment_ids": list(item.xor_segment_ids),
            "first_only_segment_ids": list(item.first_only_segment_ids),
            "second_only_segment_ids": list(item.second_only_segment_ids),
        }
        for item in plan.adjacent_algebra
    ]

    crop_records = _jsonl(stage / "inputs" / "crops.jsonl")
    assert tuple(record["block_id"] for record in crop_records) == (
        "block-000000",
        "block-000001",
    )
    for crop, record in zip(crops, crop_records):
        raw_path = stage / str(record["raw"])
        gamma_path = stage / str(record["gamma"])
        assert raw_path.read_bytes() == crop.raw.png_bytes
        assert gamma_path.read_bytes() == crop.gamma.png_bytes
        assert record["raw_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
        assert record["gamma_sha256"] == hashlib.sha256(
            gamma_path.read_bytes()
        ).hexdigest()

    job_records = _jsonl(stage / "jobs" / "jobs.jsonl")
    assert len(job_records) == len(queue.jobs)
    for job, record in zip(queue.jobs, job_records):
        assert record["job_id"] == job.job_id
        assert record["input_sha256"] == job.input_sha256
        assert record["context_sha256"] == job.context_sha256
        if job.output is None:
            assert record["text_path"] is None
        else:
            text_path = stage / str(record["text_path"])
            assert text_path.read_text(encoding="utf-8") == job.output.text + "\n"
            assert record["geometry"] == job.output.geometry.value
            assert record["words"] == [
                {
                    "text": word.text,
                    "bbox": list(word.bbox.as_tuple()),
                    "confidence": float(word.confidence),
                }
                for word in job.output.words
            ]

    selected = _jsonl(stage / "segments" / "selected.jsonl")
    assert len(selected) == len(fusion.segments)
    for index, (segment, record) in enumerate(zip(fusion.segments, selected)):
        assert Path(str(record["selected_text"])).name == (
            f"segment-{index:08d}.txt"
        )
        text_path = stage / str(record["selected_text"])
        assert text_path.read_text(encoding="utf-8") == (
            (segment.selected_text or "") + "\n"
        )
        assert record["unresolved"] is segment.unresolved
    observations = _jsonl(stage / "segments" / "observations.jsonl")
    assert [record["observation_id"] for record in observations] == [
        item.observation_id for item in fusion.observations
    ]

    block_text = _jsonl(stage / "blocks" / "text.jsonl")
    assert len(block_text) == len(fusion.block_text_observations)
    for observation, record in zip(fusion.block_text_observations, block_text):
        block_path = stage / str(record["text_path"])
        assert block_path.read_text(encoding="utf-8") == observation.text + "\n"
    unassigned = _jsonl(stage / "unassigned-words.jsonl")
    replicas = _jsonl(stage / "replica-conflicts.jsonl")
    overlaps = _jsonl(stage / "overlaps.jsonl")
    assert unassigned[0]["reason"] == "no-segment-intersection"
    assert unassigned[0]["input_sha256"] == queue.jobs[0].input_sha256
    assert [record["evidence_sha256"] for record in replicas] == [
        list(item.evidence_sha256) for item in fusion.replica_conflicts
    ]
    assert overlaps[0]["missing_intersection_segment_ids"] == list(
        fusion.overlaps[0].missing_intersection_segment_ids
    )
    assert overlaps[0][
        "cross_transform_confirmed_intersection_segment_ids"
    ] == list(
        fusion.overlaps[0].cross_transform_confirmed_intersection_segment_ids
    )
    assert overlaps[0]["near_confirmed_intersection_segment_ids"] == list(
        fusion.overlaps[0].near_confirmed_intersection_segment_ids
    )
    assert overlaps[0]["deferred_intersection_segment_ids"] == list(
        fusion.overlaps[0].deferred_intersection_segment_ids
    )

    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["semantic_stage"] == 2
    assert manifest["execution_step"] == 6
    assert manifest["jobs"] == len(queue.jobs)
    assert manifest["complete_jobs"] == queue.complete
    assert manifest["failed_jobs"] == queue.failed
    assert manifest["segment_observations"] == len(fusion.observations)
    assert manifest["block_text_observations"] == len(
        fusion.block_text_observations
    )
    assert manifest["unassigned_words"] == len(
        fusion.unassigned_word_observations
    )
    assert manifest["replica_conflicts"] == len(fusion.replica_conflicts)
    assert manifest["overlap_pairs"] == len(fusion.overlaps)
    assert manifest["overlap_exact_confirmed"] == sum(
        len(item.confirmed_intersection_segment_ids) for item in fusion.overlaps
    )
    assert manifest["overlap_cross_transform_confirmed"] == sum(
        len(item.cross_transform_confirmed_intersection_segment_ids)
        for item in fusion.overlaps
    )
    assert manifest["overlap_near_confirmed"] == sum(
        len(item.near_confirmed_intersection_segment_ids)
        for item in fusion.overlaps
    )
    assert manifest["overlap_deferred"] == sum(
        len(item.deferred_intersection_segment_ids) for item in fusion.overlaps
    )
    assert manifest["overlap_conflicts"] == sum(
        len(item.conflicting_intersection_segment_ids) for item in fusion.overlaps
    )
    assert manifest["overlap_missing"] == sum(
        len(item.missing_intersection_segment_ids) for item in fusion.overlaps
    )
    assert manifest["invariants"]["reference_evidence_forbidden"] is True
    assert manifest["invariants"]["overlap_consensus_categories_typed"] is True
    assert manifest["invariants"]["near_consensus_candidate_preserving"] is True
    assert manifest["invariants"]["deferred_segments_fail_closed"] is True


def test_writer_publishes_typed_subblock_evidence_without_forging_segments(
    tmp_path: Path,
) -> None:
    segments = (
        Segment(
            "segment-000000",
            Box(1, 1, 8, 5),
            Box(1, 1, 8, 5),
            SegmentKind.TEXT,
            20,
            0,
            (0, 1),
            ("root",),
        ),
        Segment(
            "segment-000001",
            Box(10, 1, 18, 5),
            Box(10, 1, 18, 5),
            SegmentKind.TEXT,
            20,
            0,
            (0, 10),
            ("root",),
        ),
    )
    source_ids = tuple(item.segment_id for item in segments)
    matrix = SparseSegmentMatrix(
        rows=(AxisInterval(0, 0, 30),),
        columns=(AxisInterval(0, 0, 10), AxisInterval(1, 10, 20)),
        cells=(
            SparseCell(0, 0, source_ids[0]),
            SparseCell(0, 1, source_ids[1]),
        ),
        spans=(
            SegmentSpan(source_ids[0], 0, 1, 0, 1),
            SegmentSpan(source_ids[1], 0, 1, 1, 2),
        ),
    )
    plan = BlockPlan(
        aligned_size=(20, 30),
        source_segment_ids=source_ids,
        blocks=(
            RecognitionBlock(
                "block-000000",
                Box(0, 0, 20, 10),
                source_ids,
                source_ids,
                (),
                ("object-000000",),
                "scope-000000",
            ),
        ),
        adjacent_algebra=(),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=(
            MembershipUnit(
                "membership-unit-000000",
                MembershipUnitKind.SUBBLOCK,
                source_ids,
                ("block-000000",),
                "scope-000000",
            ),
        ),
        matrix_sha256=sparse_matrix_sha256(matrix),
    )
    crops = _crops(plan)
    crop = crops[0]
    jobs = tuple(
        _complete_job(
            job_id=f"ocr-job-{index:08d}",
            crop=crop,
            transform=transform,
            output=OcrEngineOutput(
                "Left Right",
                (
                    OcrWord("Left", Box(1, 1, 8, 5), 0.95),
                    OcrWord("Right", Box(10, 1, 18, 5), 0.95),
                ),
                OcrOutputGeometry.WORD_BOXES,
            ),
        )
        for index, transform in enumerate(
            (OcrTransform.RAW, OcrTransform.GAMMA)
        )
    )
    queue = OcrQueueResult(
        jobs=jobs,
        status=OcrQueueStatus.COMPLETE,
        complete=len(jobs),
        failed=0,
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
    assert fusion.segments[0].selected_text is None
    assert fusion.segments[1].selected_text is None
    assert fusion.segment_groups[0].selected_text == "Left Right"

    destination = OcrArtifactWriter().write(
        tmp_path,
        run_id="typed-subblock",
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
        fusion=fusion,
        fusion_config=fusion_config,
        matrix=matrix,
    )
    stage = destination / "02-ocr"
    plan_record = json.loads((stage / "inputs" / "plan.json").read_bytes())
    assert plan_record["matrix_sha256"] == sparse_matrix_sha256(matrix)
    matrix_record = json.loads((stage / "inputs" / "matrix.json").read_bytes())
    assert matrix_record["sha256"] == sparse_matrix_sha256(matrix)
    assert plan_record["membership_units"] == [
        {
            "unit_id": "membership-unit-000000",
            "kind": "subblock",
            "segment_ids": list(source_ids),
            "block_ids": ["block-000000"],
            "scope_id": "scope-000000",
        }
    ]
    selected = _jsonl(stage / "groups" / "selected.jsonl")
    observations = _jsonl(stage / "groups" / "observations.jsonl")
    assert selected[0]["segment_ids"] == list(source_ids)
    assert (stage / str(selected[0]["selected_text"])).read_text(
        encoding="utf-8"
    ) == "Left Right\n"
    assert len(observations) == 2
    assert all(record["segment_ids"] == list(source_ids) for record in observations)
    manifest = json.loads((stage / "manifest.json").read_bytes())
    assert manifest["segment_groups"] == 1
    assert manifest["segment_group_observations"] == 2
    assert manifest["invariants"]["or_xor_geometry_only"] is False

    forged_matrix = SparseSegmentMatrix(
        rows=matrix.rows,
        columns=matrix.columns,
        cells=(
            SparseCell(0, 0, source_ids[1]),
            SparseCell(0, 1, source_ids[0]),
        ),
        spans=(
            SegmentSpan(source_ids[0], 0, 1, 1, 2),
            SegmentSpan(source_ids[1], 0, 1, 0, 1),
        ),
    )
    assert forged_matrix.segment_ids() == matrix.segment_ids()
    assert sparse_matrix_sha256(forged_matrix) != plan.matrix_sha256
    with pytest.raises(ValueError, match="matrix disagrees"):
        OcrArtifactWriter().write(
            tmp_path,
            run_id="forged-spatial-matrix",
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
            fusion=fusion,
            fusion_config=fusion_config,
            matrix=forged_matrix,
        )
    assert not (tmp_path / "forged-spatial-matrix").exists()


def test_selection_manifest_cannot_leak_reference_path_or_text(
    tmp_path: Path,
) -> None:
    plan, segments, crops, queue, fusion = _evidence()
    destination = OcrArtifactWriter().write(
        tmp_path,
        run_id="no-reference-leak",
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
        fusion=fusion,
    )
    stage = destination / "02-ocr"
    selection_payload = "\n".join(
        (stage / relative).read_text(encoding="utf-8")
        for relative in (
            "manifest.json",
            "segments/selected.jsonl",
            "diagnostics.txt",
        )
    )
    assert "SECRET_REFERENCE_TEXT_7f62" not in selection_payload
    assert "/private/reference-answer.txt" not in selection_payload
    assert "reference_path" not in selection_payload
    assert "reference_text" not in selection_payload


def test_writer_never_overwrites_a_published_run(tmp_path: Path) -> None:
    plan, segments, crops, queue, fusion = _evidence()
    writer = OcrArtifactWriter()
    first = writer.write(
        tmp_path,
        run_id="immutable-run",
        plan=plan,
        segments=segments,
        crops=crops,
        queue=queue,
        fusion=fusion,
    )
    manifest_before = (first / "02-ocr" / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        writer.write(
            tmp_path,
            run_id="immutable-run",
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
            fusion=fusion,
        )
    assert (first / "02-ocr" / "manifest.json").read_bytes() == manifest_before


def test_writer_rolls_back_partial_tree_on_any_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, segments, crops, queue, fusion = _evidence()

    def fail(stage: Path, **_kwargs: object) -> None:
        stage.mkdir(parents=True)
        (stage / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected artifact failure")

    monkeypatch.setattr(OcrArtifactWriter, "_write_stage", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        OcrArtifactWriter().write(
            tmp_path,
            run_id="rollback-run",
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
            fusion=fusion,
        )
    assert not (tmp_path / "rollback-run").exists()
    assert not tuple(tmp_path.glob(".rollback-run.partial-*"))


def test_concurrent_same_run_has_one_atomic_winner(tmp_path: Path) -> None:
    plan, segments, crops, queue, fusion = _evidence()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def publish() -> None:
        barrier.wait()
        try:
            outcome: object = OcrArtifactWriter().write(
                tmp_path,
                run_id="racing-run",
                plan=plan,
                segments=segments,
                crops=crops,
                queue=queue,
                fusion=fusion,
            )
        except Exception as exc:
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = tuple(threading.Thread(target=publish) for _ in range(2))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    assert sum(isinstance(item, Path) for item in outcomes) == 1
    losers = tuple(item for item in outcomes if isinstance(item, Exception))
    assert len(losers) == 1
    assert isinstance(losers[0], OSError)
    assert (tmp_path / "racing-run" / "02-ocr" / "manifest.json").is_file()
    assert not tuple(tmp_path.glob(".racing-run.partial-*"))


def test_writer_rejects_forged_job_digest_instead_of_publishing_it(
    tmp_path: Path,
) -> None:
    plan, segments, crops, queue, fusion = _evidence()
    forged_job = replace(queue.jobs[0], input_sha256="f" * 64)
    forged_queue = OcrQueueResult(
        jobs=(forged_job, *queue.jobs[1:]),
        status=queue.status,
        complete=queue.complete,
        failed=queue.failed,
        diagnostics=queue.diagnostics,
    )
    with pytest.raises(ValueError, match="digest|sha|crop|input"):
        OcrArtifactWriter().write(
            tmp_path,
            run_id="forged-digest",
            plan=plan,
            segments=segments,
            crops=crops,
            queue=forged_queue,
            fusion=fusion,
        )
    assert not (tmp_path / "forged-digest").exists()
