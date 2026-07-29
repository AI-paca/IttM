from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageOps

from app.services.sparse_convert_service import SparseConvertService
from app.sparse_pipeline import SparsePipelineRuntime as LazyRuntimeExport
from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    BlockPlanningMode,
)
from app.sparse_pipeline.contracts import Box, SparseCoordinateMode
from app.sparse_pipeline.document_assembly import AssemblyStatus
from app.sparse_pipeline.ocr_adapter_contracts import OcrResource
from app.sparse_pipeline.ocr_fusion import (
    OcrFusionConfig,
    OcrFusionStatus,
    OcrRoutingMode,
)
from app.sparse_pipeline.ocr_queue import OcrEngineOutput, OcrLane, OcrWord
from app.sparse_pipeline.pipeline_control import PIPELINE_ORDER
from app.sparse_pipeline.runtime import (
    SPARSE_RUNTIME_PROFILE,
    SparsePipelineRuntime,
    SparseRuntimeConfig,
    resolve_sparse_runtime_profile,
)


class _InkWorker:
    def __init__(self, state: dict[str, int], *, confidence: float = 1.0) -> None:
        self._state = state
        self._confidence = confidence

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        self._state["recognize"] += 1
        with Image.open(io.BytesIO(png_bytes)) as opened:
            grayscale = opened.convert("L")
        try:
            inverted = ImageOps.invert(grayscale)
            try:
                bbox = inverted.point(lambda value: 255 if value > 32 else 0).getbbox()
            finally:
                inverted.close()
        finally:
            grayscale.close()
        if bbox is None:
            return OcrEngineOutput(text="", words=())
        return OcrEngineOutput(
            text="HELLO",
            words=(
                OcrWord(
                    text="HELLO",
                    bbox=Box(*bbox),
                    confidence=self._confidence,
                ),
            ),
        )

    def close(self) -> None:
        self._state["close"] += 1


def _page() -> Image.Image:
    image = Image.new("RGB", (240, 100), "white")
    draw = ImageDraw.Draw(image)
    for left in (30, 50, 72, 100, 120, 145, 170):
        draw.rectangle((left, 35, left + 10, 60), fill="black")
    return image


def _lane(
    state: dict[str, int],
    *,
    confidence: float = 1.0,
) -> OcrLane:
    def factory() -> _InkWorker:
        state["factory"] += 1
        return _InkWorker(state, confidence=confidence)

    return OcrLane(
        lane_id="fake-bbox-cpu",
        resource=OcrResource.CPU,
        max_workers=1,
        worker_factory=factory,
        capability_id="fake-bbox-v1",
    )


def test_runtime_executes_frozen_order_and_returns_certified_evidence() -> None:
    state = {"factory": 0, "recognize": 0, "close": 0}
    image = _page()
    original = image.tobytes()

    with SparsePipelineRuntime((_lane(state),)) as runtime:
        result = runtime.process_page(image, page_id="page-001")

        assert result.completed_stages == PIPELINE_ORDER == (3, 1, 6, 4, 5, 2, 7)
        assert result.document.status is AssemblyStatus.COMPLETE
        assert result.document.text == "HELLO"
        assert result.evidence.fusion.status is OcrFusionStatus.COMPLETE
        assert result.evidence.queue.failed == 0
        assert result.evidence.stage4 is None
        assert result.evidence.geometry.aligned_rgb_sha256
        assert (
            result.evidence.geometry.matrix.coordinate_mode
            is SparseCoordinateMode.PIXEL_PARTITION
        )
        assert state["factory"] == 1
        assert state["recognize"] == len(result.evidence.queue.jobs) == 2

    assert runtime.closed is True
    assert state["close"] == 1
    assert image.tobytes() == original
    assert image.getpixel((0, 0)) == (255, 255, 255)
    image.close()


def test_service_reuses_one_thread_affine_worker_across_pages_and_closes_once() -> None:
    state = {"factory": 0, "recognize": 0, "close": 0}
    first = _page()
    second = _page()
    service = SparseConvertService((_lane(state),))
    try:
        first_result = service.convert_page(first, page_id="document-p001")
        second_result = service.convert_page(second, page_id="document-p002")

        expected_jobs = len(first_result.evidence.queue.jobs) + len(
            second_result.evidence.queue.jobs
        )
        assert state == {
            "factory": 1,
            "recognize": expected_jobs,
            "close": 0,
        }
        assert first_result.document.text == second_result.document.text == "HELLO"
    finally:
        service.close()
        service.close()
        first.close()
        second.close()

    assert service.closed is True
    assert state["close"] == 1
    closed_page = _page()
    try:
        with pytest.raises(RuntimeError, match="closed"):
            service.convert_page(closed_page, page_id="document-p003")
    finally:
        closed_page.close()


def test_runtime_returns_unresolved_evidence_without_certifying_low_confidence_text() -> None:
    state = {"factory": 0, "recognize": 0, "close": 0}
    image = _page()
    try:
        with SparsePipelineRuntime((_lane(state, confidence=0.1),)) as runtime:
            result = runtime.process_page(image, page_id="low-confidence")

        assert result.evidence.fusion.status is OcrFusionStatus.UNRESOLVED
        assert result.document.status is AssemblyStatus.UNRESOLVED
        assert result.document.candidate_text == "HELLO"
        assert result.document.text is None
    finally:
        image.close()


def test_blank_page_completes_without_starting_an_ocr_worker() -> None:
    state = {"factory": 0, "recognize": 0, "close": 0}
    image = Image.new("RGB", (40, 40), "white")
    try:
        with SparsePipelineRuntime((_lane(state),)) as runtime:
            result = runtime.process_page(image, page_id="blank-page")

        assert result.document.status is AssemblyStatus.COMPLETE
        assert result.document.text == ""
        assert result.evidence.plan.blocks == ()
        assert result.evidence.queue.jobs == ()
        assert state == {"factory": 0, "recognize": 0, "close": 0}
    finally:
        image.close()


def test_profile_resolution_is_separate_and_fail_closed() -> None:
    profile = resolve_sparse_runtime_profile()
    direct = SparseRuntimeConfig()

    assert LazyRuntimeExport is SparsePipelineRuntime
    assert isinstance(profile, SparseRuntimeConfig)
    assert profile.profile_name == SPARSE_RUNTIME_PROFILE
    assert profile.block_planning.mode is BlockPlanningMode.SPATIAL_2D
    assert direct.block_planning.mode is BlockPlanningMode.SPATIAL_2D
    assert profile.ocr_fusion.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
    assert direct.ocr_fusion.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
    assert profile.ocr_fusion.membership_assume_complete_observations is True
    assert profile.block_planning.padding == 24
    assert profile.block_planning.object_local is False
    assert profile.block_planning.adaptive_table_windows is True
    with pytest.raises(ValueError, match="Unknown sparse runtime profile"):
        resolve_sparse_runtime_profile("legacy-profile-is-not-sparse")


@pytest.mark.parametrize(
    ("planning", "fusion"),
    (
        (
            BlockPlanningConfig(mode=BlockPlanningMode.SPATIAL_2D),
            OcrFusionConfig(routing_mode=OcrRoutingMode.BBOX_INTERSECTION),
        ),
        (
            BlockPlanningConfig(mode=BlockPlanningMode.FULL_WIDTH),
            OcrFusionConfig(routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP),
        ),
    ),
)
def test_runtime_rejects_inconsistent_planning_and_routing_profiles(
    planning: BlockPlanningConfig,
    fusion: OcrFusionConfig,
) -> None:
    with pytest.raises(ValueError, match="enabled together"):
        SparseRuntimeConfig(block_planning=planning, ocr_fusion=fusion)


@pytest.mark.parametrize("page_id", ("", "../escape", "contains space", "x" * 129))
def test_runtime_rejects_unsafe_page_identifiers_before_image_work(
    page_id: str,
) -> None:
    state = {"factory": 0, "recognize": 0, "close": 0}
    image = _page()
    try:
        with SparsePipelineRuntime((_lane(state),)) as runtime:
            with pytest.raises(ValueError, match="page_id"):
                runtime.process_page(image, page_id=page_id)
        assert state == {"factory": 0, "recognize": 0, "close": 0}
    finally:
        image.close()
