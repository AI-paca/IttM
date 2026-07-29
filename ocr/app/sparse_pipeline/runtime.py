"""Production orchestration for one already-decoded document page.

The legacy conversion service grew around region-specific shortcuts.  This
module deliberately exposes a separate boundary for the recursive sparse
pipeline so callers can opt in without changing the existing API contract.
One :class:`SparsePipelineRuntime` owns one persistent OCR session and can be
reused for every page of a document.
"""

from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass, field

from PIL import Image

from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import SparseCoordinateMode
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.document_assembly import (
    DocumentAssembler,
    DocumentAssemblyConfig,
    DocumentAssemblyResult,
)
from app.sparse_pipeline.geometry import (
    GeometryAnalyzer,
    GeometryBundle,
    GeometryConfig,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectReconstructionConfig,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrRoutingMode,
)
from app.sparse_pipeline.ocr_queue import OcrLane, OcrQueueConfig
from app.sparse_pipeline.ocr_session import PersistentOcrSession
from app.sparse_pipeline.pipeline_control import (
    PIPELINE_ORDER,
    run_pipeline_control,
)
from app.sparse_pipeline.pipeline_evidence import SparsePipelineEvidence
from app.sparse_pipeline.recursive_control import RunOutcome, RunStatus

SPARSE_RUNTIME_PROFILE = "sparse_v20_standard"
_SAFE_PAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _standard_block_planning() -> BlockPlanningConfig:
    return BlockPlanningConfig(
        mode=BlockPlanningMode.SPATIAL_2D,
        padding=24,
        object_local=False,
        adaptive_table_windows=True,
    )


def _standard_ocr_fusion() -> OcrFusionConfig:
    # This is an explicit experimental profile decision, not the global
    # OcrFusionConfig default.  Fusion verifies a complete RAW/GAMMA job matrix
    # before it is permitted to treat missing word observations as negative bits.
    return OcrFusionConfig(
        routing_mode=OcrRoutingMode.BLOCK_MEMBERSHIP,
        membership_assume_complete_observations=True,
    )


@dataclass(frozen=True)
class SparseRuntimeConfig:
    """Immutable production limits for all sparse stages.

    Image enhancement is block-local and configured through ``block_crops``.
    The aligned page used by geometry and object reconstruction is never sent
    through the enhancer.
    """

    profile_name: str = SPARSE_RUNTIME_PROFILE
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    objects: ObjectReconstructionConfig = field(
        default_factory=ObjectReconstructionConfig
    )
    block_planning: BlockPlanningConfig = field(
        default_factory=_standard_block_planning
    )
    block_crops: BlockCropConfig = field(default_factory=BlockCropConfig)
    ocr_queue: OcrQueueConfig = field(default_factory=OcrQueueConfig)
    ocr_fusion: OcrFusionConfig = field(default_factory=_standard_ocr_fusion)
    document_assembly: DocumentAssemblyConfig = field(
        default_factory=DocumentAssemblyConfig
    )

    def __post_init__(self) -> None:
        if (
            type(self.profile_name) is not str
            or not self.profile_name
            or not _SAFE_PAGE_ID.fullmatch(self.profile_name)
        ):
            raise ValueError("sparse runtime profile name is invalid")
        expected = (
            ("geometry", self.geometry, GeometryConfig),
            ("objects", self.objects, ObjectReconstructionConfig),
            ("block_planning", self.block_planning, BlockPlanningConfig),
            ("block_crops", self.block_crops, BlockCropConfig),
            ("ocr_queue", self.ocr_queue, OcrQueueConfig),
            ("ocr_fusion", self.ocr_fusion, OcrFusionConfig),
            (
                "document_assembly",
                self.document_assembly,
                DocumentAssemblyConfig,
            ),
        )
        for name, value, value_type in expected:
            if not isinstance(value, value_type):
                raise TypeError(f"{name} must be a {value_type.__name__}")
        spatial = self.block_planning.mode is BlockPlanningMode.SPATIAL_2D
        membership = (
            self.ocr_fusion.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
        )
        if spatial != membership:
            raise ValueError(
                "spatial block planning and membership OCR routing must be "
                "enabled together"
            )
        if (
            self.ocr_fusion.membership_assume_complete_observations
            and not membership
        ):
            raise ValueError(
                "complete membership observations require membership routing"
            )


SPARSE_RUNTIME_PROFILES: dict[str, SparseRuntimeConfig] = {
    SPARSE_RUNTIME_PROFILE: SparseRuntimeConfig(),
}


def resolve_sparse_runtime_profile(
    profile_name: str | None = None,
) -> SparseRuntimeConfig:
    """Resolve a sparse profile independently from legacy OCR profiles."""

    name = profile_name or SPARSE_RUNTIME_PROFILE
    profile = SPARSE_RUNTIME_PROFILES.get(name)
    if profile is None:
        known = ", ".join(sorted(SPARSE_RUNTIME_PROFILES))
        raise ValueError(
            f"Unknown sparse runtime profile '{name}'. Known profiles: {known}"
        )
    return profile


@dataclass(frozen=True)
class SparsePageResult:
    """The certified Stage 7 result and its complete immutable provenance."""

    page_id: str
    completed_stages: tuple[int, ...]
    control: RunOutcome
    document: DocumentAssemblyResult
    evidence: SparsePipelineEvidence

    def __post_init__(self) -> None:
        if type(self.page_id) is not str or not _SAFE_PAGE_ID.fullmatch(
            self.page_id
        ):
            raise ValueError("sparse page identifier is invalid")
        if self.completed_stages != PIPELINE_ORDER:
            raise ValueError("sparse page stages are incomplete or reordered")
        if not isinstance(self.control, RunOutcome):
            raise TypeError("control must be a RunOutcome")
        if self.control.status is not RunStatus.COMPLETE:
            raise ValueError("sparse page control did not complete")
        try:
            control_order = tuple(
                int(atom.payload.split(":", 1)[0])
                for atom in self.control.evidence
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("sparse page control evidence is invalid") from exc
        if control_order != self.completed_stages:
            raise ValueError("sparse page control evidence reordered the stages")
        if not isinstance(self.document, DocumentAssemblyResult):
            raise TypeError("document must be a DocumentAssemblyResult")
        if not isinstance(self.evidence, SparsePipelineEvidence):
            raise TypeError("evidence must be a SparsePipelineEvidence")
        if self.evidence.page.crop_id != f"{self.page_id}-page":
            raise ValueError("sparse page identifier disagrees with its evidence")
        if (
            self.document.source_segment_ids
            != self.evidence.fusion.source_segment_ids
            or self.document.source_object_ids
            != tuple(item.object_id for item in self.evidence.objects.objects)
            or self.document.source_block_ids
            != tuple(item.block_id for item in self.evidence.plan.blocks)
        ):
            raise ValueError("Stage 7 output disagrees with sparse evidence")


class SparsePipelineRuntime:
    """Own one persistent OCR session and execute pages in frozen stage order.

    The caller owns ``image``.  ``process_page`` neither mutates nor closes it.
    Runtime calls are serialized with ``close`` so a page cannot lose its OCR
    workers halfway through execution.  Worker instances are created lazily by
    :class:`PersistentOcrSession` and remain thread-affine until ``close``.
    """

    def __init__(
        self,
        lanes: tuple[OcrLane, ...],
        config: SparseRuntimeConfig | None = None,
    ) -> None:
        if type(lanes) is not tuple or any(
            not isinstance(lane, OcrLane) for lane in lanes
        ):
            raise TypeError("lanes must be an immutable OcrLane tuple")
        if not lanes:
            raise ValueError("sparse runtime requires at least one OCR lane")
        if config is not None and not isinstance(config, SparseRuntimeConfig):
            raise TypeError("config must be a SparseRuntimeConfig")
        self.config = config or resolve_sparse_runtime_profile()
        if len(lanes) > self.config.ocr_queue.max_lanes:
            raise ValueError("sparse runtime OCR lane count exceeds the queue limit")
        if any(
            lane.max_workers > self.config.ocr_queue.max_pending_per_lane
            for lane in lanes
        ):
            raise ValueError(
                "sparse runtime lane workers exceed the per-lane queue limit"
            )
        self.lanes = lanes
        self._session = PersistentOcrSession(
            lanes,
            config=self.config.ocr_queue,
        )
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    def __enter__(self) -> SparsePipelineRuntime:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("sparse runtime is closed")
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.close()

    def process_page(
        self,
        image: Image.Image,
        *,
        page_id: str,
    ) -> SparsePageResult:
        """Run one decoded Pillow page through stages 3, 1, 6, 4, 5, 2, 7."""

        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image")
        if type(page_id) is not str or not _SAFE_PAGE_ID.fullmatch(page_id):
            raise ValueError("page_id must be a safe identifier of at most 128 characters")
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("sparse runtime is closed")
            return self._process_owned_page(image, page_id=page_id)

    def _process_owned_page(
        self,
        image: Image.Image,
        *,
        page_id: str,
    ) -> SparsePageResult:
        # Stage 3: execute the real bounded recursive control schedule first.
        control = run_pipeline_control()

        # Stage 1: align, recursively segment, and build the sparse matrix.
        geometry_bundle = GeometryAnalyzer(self.config.geometry).analyze_bundle(
            image
        )
        geometry = geometry_bundle.result
        if geometry.matrix.coordinate_mode is not SparseCoordinateMode.PIXEL_PARTITION:
            raise RuntimeError(
                "production Stage 1 requires a physical pixel partition; "
                "legacy recursive alignment projections are diagnostic-only"
            )

        # Stage 6: derive bounded structural objects from sparse evidence only.
        objects = ObjectReconstructor(self.config.objects).reconstruct(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            rules=geometry.segmentation.rules,
            matrix=geometry.matrix,
        )

        page = CropInput(
            f"{page_id}-page",
            _aligned_png_bytes(
                geometry_bundle,
                dpi=self.config.block_crops.dpi,
            ),
        )

        # Finalize all block memberships and coordinates before any image
        # enhancement.  BlockCropper then derives RAW/GAMMA OCR candidates
        # independently for each immutable block crop.
        plan = OverlappingBlockPlanner(self.config.block_planning).plan(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            objects_result=objects,
            matrix=geometry.matrix,
        )
        crops, aligned_rgb_sha256 = BlockCropper(
            self.config.block_crops
        ).crop_with_rgb_sha256(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        if aligned_rgb_sha256 != geometry.aligned_rgb_sha256:
            raise RuntimeError("Stage 1 and Stage 5 aligned RGB digests disagree")

        # Stage 2: reuse persistent, thread-affine workers and fuse observed text.
        queue = self._session.run(plan=plan, crops=crops)
        fusion = OcrEvidenceFusion(self.config.ocr_fusion).fuse(
            plan=plan,
            segments=geometry.segmentation.segments,
            crops=crops,
            queue=queue,
        )
        evidence = SparsePipelineEvidence(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            stage4=None,
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
            object_config=self.config.objects,
            planning_config=self.config.block_planning,
            crop_config=self.config.block_crops,
            fusion_config=self.config.ocr_fusion,
            stage4_config=None,
        )

        # Stage 7: assemble only after cross-stage provenance has been certified.
        document = DocumentAssembler(self.config.document_assembly).assemble(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
            object_config=self.config.objects,
            planning_config=self.config.block_planning,
            crop_config=self.config.block_crops,
            fusion_config=self.config.ocr_fusion,
        )
        return SparsePageResult(
            page_id=page_id,
            completed_stages=PIPELINE_ORDER,
            control=control,
            document=document,
            evidence=evidence,
        )

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._session.close()
            self._closed = True


def _aligned_png_bytes(bundle: GeometryBundle, *, dpi: int) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(bundle.aligned_rgb, mode="RGB")
    try:
        image.save(
            output,
            format="PNG",
            compress_level=9,
            optimize=False,
            dpi=(dpi, dpi),
        )
    finally:
        image.close()
    return output.getvalue()


__all__ = [
    "SPARSE_RUNTIME_PROFILE",
    "SPARSE_RUNTIME_PROFILES",
    "SparsePageResult",
    "SparsePipelineRuntime",
    "SparseRuntimeConfig",
    "resolve_sparse_runtime_profile",
]
