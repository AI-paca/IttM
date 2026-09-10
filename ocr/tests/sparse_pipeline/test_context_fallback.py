from __future__ import annotations

from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
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
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionResult,
    SegmentObjectOwnership,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrOutputGeometry,
    OcrResource,
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
from app.sparse_pipeline.runtime import (
    _context_fallback_selected,
    _select_context_fallback_queue,
)


def _segments(count: int) -> tuple[Segment, ...]:
    return tuple(
        Segment(
            segment_id=f"segment-{index:06d}",
            bbox=Box(10, 10 + index * 20, 90, 20 + index * 20),
            source_bbox=Box(10, 10 + index * 20, 90, 20 + index * 20),
            kind=SegmentKind.TEXT,
            ink_pixels=10,
            row_index=index,
            order_key=(index, 0),
            parent_path=("root",),
            component_ids=(index,),
        )
        for index in range(count)
    )


def _matrix(segments: tuple[Segment, ...]) -> SparseSegmentMatrix:
    rows = tuple(AxisInterval(index, index * 20, (index + 1) * 20) for index in range(len(segments)))
    columns = (AxisInterval(0, 0, 120),)
    cells = tuple(SparseCell(index, 0, segment.segment_id) for index, segment in enumerate(segments))
    spans = tuple(SegmentSpan(segment.segment_id, index, index + 1, 0, 1) for index, segment in enumerate(segments))
    return SparseSegmentMatrix(rows, columns, cells, spans)


def _objects(
    segments: tuple[Segment, ...],
    *,
    kind: ObjectKind,
    confidence: float,
) -> ObjectReconstructionResult:
    objects = tuple(
        DocumentObject(
            object_id=f"object-{index:06d}",
            kind=kind,
            segment_ids=(segment.segment_id,),
            bbox=segment.bbox,
            reading_index=index,
            row_start=index,
            row_stop=index + 1,
            column_start=0,
            column_stop=1,
            confidence=confidence,
        )
        for index, segment in enumerate(segments)
    )
    return ObjectReconstructionResult(
        aligned_size=(120, 100),
        source_segment_ids=tuple(item.segment_id for item in segments),
        objects=objects,
        segment_ownership=tuple(
            SegmentObjectOwnership(
                segment.segment_id,
                f"object-{index:06d}",
            )
            for index, segment in enumerate(segments)
        ),
    )


def _fallback_plan():
    segments = _segments(4)
    objects = _objects(
        segments,
        kind=ObjectKind.UNKNOWN,
        confidence=0.0,
    )
    plan = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            adaptive_table_windows=True,
            context_fallback_enabled=True,
            context_fallback_minimum_area_fraction=0.05,
            context_fallback_minimum_page_pixels=1,
        )
    ).plan(
        aligned_size=(120, 100),
        segments=segments,
        objects_result=objects,
        matrix=_matrix(segments),
    )
    return segments, objects, plan


def test_unconfirmed_topology_marks_exactly_one_existing_whole_object() -> None:
    segments, objects, plan = _fallback_plan()
    baseline = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            adaptive_table_windows=True,
            context_fallback_enabled=False,
            context_fallback_minimum_area_fraction=0.05,
            context_fallback_minimum_page_pixels=1,
        )
    ).plan(
        aligned_size=(120, 100),
        segments=segments,
        objects_result=objects,
        matrix=_matrix(segments),
    )

    assert objects.topology_confidence == 0.0
    assert not objects.is_topology_confirmed(minimum_confidence=0.8)
    fallback = tuple(item for item in plan.blocks if item.context_fallback)
    assert len(fallback) == 1
    assert fallback[0].segment_ids == objects.objects[0].segment_ids
    assert fallback[0].core_segment_ids == fallback[0].segment_ids
    assert fallback[0].bbox == baseline.blocks[0].bbox
    assert tuple(item.segment_ids for item in plan.blocks) == tuple(item.segment_ids for item in baseline.blocks)
    assert plan.membership_units == baseline.membership_units
    assert len(plan.blocks) == len(baseline.blocks)
    assert "context-fallback=selected-whole-object" in plan.diagnostics


def test_confirmed_table_never_receives_context_fallback() -> None:
    segments = _segments(1)
    objects = _objects(
        segments,
        kind=ObjectKind.TABLE,
        confidence=0.9,
    )
    plan = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            adaptive_table_windows=False,
            context_fallback_enabled=True,
        )
    ).plan(
        aligned_size=(120, 100),
        segments=segments,
        objects_result=objects,
        matrix=_matrix(segments),
    )

    assert objects.has_confirmed_table(minimum_confidence=0.8)
    assert not any(item.context_fallback for item in plan.blocks)
    assert "context-fallback=blocked-confirmed-table" in plan.diagnostics


def test_multi_membership_whole_object_does_not_activate_fallback() -> None:
    segments = _segments(4)
    segment_ids = tuple(item.segment_id for item in segments)
    objects = ObjectReconstructionResult(
        aligned_size=(120, 100),
        source_segment_ids=segment_ids,
        objects=(
            DocumentObject(
                object_id="object-000000",
                kind=ObjectKind.UNKNOWN,
                segment_ids=segment_ids,
                bbox=Box.union(item.bbox for item in segments),
                reading_index=0,
                row_start=0,
                row_stop=4,
                column_start=0,
                column_stop=1,
                confidence=0.0,
            ),
        ),
        segment_ownership=tuple(
            SegmentObjectOwnership(
                segment.segment_id,
                "object-000000",
            )
            for segment in segments
        ),
    )
    baseline = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            adaptive_table_windows=True,
            context_fallback_enabled=False,
            context_fallback_minimum_area_fraction=0.05,
            context_fallback_minimum_page_pixels=1,
        )
    ).plan(
        aligned_size=(120, 100),
        segments=segments,
        objects_result=objects,
        matrix=_matrix(segments),
    )
    plan = OverlappingBlockPlanner(
        BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            padding=0,
            adaptive_table_windows=True,
            context_fallback_enabled=True,
            context_fallback_minimum_area_fraction=0.05,
            context_fallback_minimum_page_pixels=1,
        )
    ).plan(
        aligned_size=(120, 100),
        segments=segments,
        objects_result=objects,
        matrix=_matrix(segments),
    )

    assert not any(item.context_fallback for item in plan.blocks)
    assert tuple(item.segment_ids for item in plan.blocks) == tuple(item.segment_ids for item in baseline.blocks)
    assert plan.membership_units == baseline.membership_units
    assert len(plan.blocks) * 2 == len(baseline.blocks) * 2
    assert "context-fallback=blocked-no-reusable-whole-object-evidence" in plan.diagnostics


def test_runtime_keeps_one_best_agreed_fallback_output() -> None:
    _, _, plan = _fallback_plan()
    raw = OcrEngineOutput(
        "alpha beta",
        (
            OcrWord("alpha", Box(1, 1, 10, 5), 0.95),
            OcrWord("beta", Box(11, 1, 20, 5), 0.95),
        ),
    )
    gamma = OcrEngineOutput(
        "alpha beta gamma",
        (
            OcrWord("alpha", Box(1, 1, 10, 5), 0.8),
            OcrWord("beta", Box(11, 1, 20, 5), 0.8),
            OcrWord("gamma", Box(21, 1, 30, 5), 0.8),
        ),
    )
    queue = OcrQueueResult(
        jobs=(
            OcrJobResult(
                "job-raw",
                "block-000000",
                OcrTransform.RAW,
                "tesseract",
                OcrResource.CPU,
                OcrJobStatus.COMPLETE,
                raw,
                None,
                None,
                0.1,
            ),
            OcrJobResult(
                "job-gamma",
                "block-000000",
                OcrTransform.GAMMA,
                "tesseract",
                OcrResource.CPU,
                OcrJobStatus.COMPLETE,
                gamma,
                None,
                None,
                0.1,
            ),
        ),
        status=OcrQueueStatus.COMPLETE,
        complete=2,
        failed=0,
    )

    selected = _select_context_fallback_queue(plan=plan, queue=queue)

    assert selected.jobs[0].output == raw
    assert selected.jobs[1].output == OcrEngineOutput(
        "alpha beta gamma",
        (),
        OcrOutputGeometry.TEXT_ONLY,
    )
    assert selected.diagnostics[-1] == ("context-fallback-selected=job-gamma;candidates=2")
    assert _context_fallback_selected(selected)


def test_runtime_does_not_activate_fallback_without_existing_output() -> None:
    _, _, plan = _fallback_plan()
    queue = OcrQueueResult(
        jobs=(
            OcrJobResult(
                "job-raw",
                "block-000000",
                OcrTransform.RAW,
                "tesseract",
                OcrResource.CPU,
                OcrJobStatus.FAILED,
                None,
                "OcrRecognitionMissError",
                "no attributable words",
                0.1,
            ),
        ),
        status=OcrQueueStatus.PARTIAL,
        complete=0,
        failed=1,
    )

    selected = _select_context_fallback_queue(plan=plan, queue=queue)

    assert selected.jobs == queue.jobs
    assert selected.diagnostics[-1] == ("context-fallback-selected=none;candidates=0")
    assert not _context_fallback_selected(selected)
