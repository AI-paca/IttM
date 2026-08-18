from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    BlockPlanningInvariantError,
    _clamped_spatial_bbox,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import Box, GeometryResult, SparseSegmentMatrix
from app.sparse_pipeline.crop_enhancement import (
    PNG_SIGNATURE,
    CropEnhancementConfig,
    CropInput,
    EnhancedCrop,
    EnhancementBackend,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectReconstructionConfig,
    ObjectReconstructionResult,
)
from app.sparse_pipeline.ocr_fusion import OcrFusionConfig, OcrFusionResult
from app.sparse_pipeline.ocr_queue import (
    OcrJobResult,
    OcrQueueResult,
    OcrTransform,
)


class SparsePipelineEvidenceInvariantError(ValueError):
    """Raised when immutable results from different sparse runs are mixed."""


@dataclass
class _EvidenceBudget:
    maximum: int
    checks: int = 0

    def consume(self, count: int) -> None:
        if self.checks + count > self.maximum:
            raise SparsePipelineEvidenceInvariantError(
                "sparse evidence revalidation exceeds the recorded work limit"
            )
        self.checks += count


def _validate_segment_handoff(
    stage1_ids: tuple[str, ...],
    stage6_ids: tuple[str, ...],
    stage5_ids: tuple[str, ...],
    stage2_ids: tuple[str, ...],
) -> None:
    """Join recursive geometry to canonical document order without forgery."""

    if (
        len(stage1_ids) != len(set(stage1_ids))
        or set(stage6_ids) != set(stage1_ids)
    ):
        raise SparsePipelineEvidenceInvariantError(
            "stage 1/6 segment identifiers disagree"
        )
    # Stage 6 establishes canonical document reading order.  Recursive Stage
    # 1 order is a tree traversal and may legitimately differ once a ruled
    # table contains merged cells.  Downstream stages must preserve Stage 6.
    if stage5_ids != stage6_ids or stage2_ids != stage6_ids:
        raise SparsePipelineEvidenceInvariantError(
            "stage 6/5/2 canonical segment order disagrees"
        )


@dataclass(frozen=True)
class SparsePipelineEvidence:
    """One provenance-checked hand-off for sparse stages 1/6/5/4/2.

    Validation is deliberately structural.  It checks immutable identifiers,
    canvas geometry and recorded digests, but never re-runs segmentation,
    object reconstruction, block planning, crop enhancement or OCR fusion.
    """

    page: CropInput
    geometry: GeometryResult
    objects: ObjectReconstructionResult
    plan: BlockPlan
    crops: tuple[BlockCropPair, ...]
    queue: OcrQueueResult
    fusion: OcrFusionResult
    stage4: EnhancedCrop | None = None
    object_config: ObjectReconstructionConfig | None = None
    planning_config: BlockPlanningConfig | None = None
    crop_config: BlockCropConfig | None = None
    fusion_config: OcrFusionConfig | None = None
    stage4_config: CropEnhancementConfig | None = None
    ownership: np.ndarray | None = None
    ownership_segment_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        self._validate_types()
        if self.ownership is not None and self.ownership.flags.writeable:
            immutable_ownership = np.array(self.ownership, copy=True)
            immutable_ownership.setflags(write=False)
            object.__setattr__(self, "ownership", immutable_ownership)

        object_config = self.object_config or ObjectReconstructionConfig()
        planning_config = self.planning_config or BlockPlanningConfig()
        crop_config = self.crop_config or BlockCropConfig()
        fusion_config = self.fusion_config or OcrFusionConfig()
        stage4_config = self.stage4_config or CropEnhancementConfig()

        segments = self.geometry.segmentation.segments
        segment_ids = tuple(item.segment_id for item in segments)
        aligned_size = self.geometry.segmentation.aligned_size
        if (
            self.objects.aligned_size != aligned_size
            or self.plan.aligned_size != aligned_size
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 1/6/5 aligned canvas sizes disagree"
            )
        _validate_segment_handoff(
            segment_ids,
            self.objects.source_segment_ids,
            self.plan.source_segment_ids,
            self.fusion.source_segment_ids,
        )

        self._validate_limits(
            object_config=object_config,
            planning_config=planning_config,
            crop_config=crop_config,
            fusion_config=fusion_config,
            stage4_config=stage4_config,
        )
        owner_by_segment = self._validate_objects(segments=segments)
        evidence_budget = _EvidenceBudget(
            planning_config.max_evidence_revalidation_checks
        )
        self._validate_plan(
            segments=segments,
            owner_by_segment=owner_by_segment,
            planning_config=planning_config,
            matrix=self.geometry.matrix,
            budget=evidence_budget,
        )
        if (
            self.plan.mode is BlockPlanningMode.SPATIAL_2D
            and self.ownership is None
        ):
            raise SparsePipelineEvidenceInvariantError(
                "spatial evidence requires the Stage 1 ownership raster"
            )
        try:
            BlockCropper._validate_exact_spatial_membership(
                aligned_size=aligned_size,
                plan=self.plan,
                ownership=self.ownership,
                ownership_segment_ids=self.ownership_segment_ids,
                isolation_source=self.crops,
            )
        except (TypeError, ValueError) as exc:
            raise SparsePipelineEvidenceInvariantError(
                f"stage 5 isolation disagrees with Stage 1 ownership: {exc}"
            ) from exc

        page_rgb = self._decode_rgb(
            self.page.png_bytes,
            expected_size=aligned_size,
            label="aligned page",
        )
        try:
            if self._rgb_sha256(page_rgb) != self.geometry.aligned_rgb_sha256:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 1 aligned RGB digest disagrees with the supplied page"
                )
            crop_by_block = self._validate_crops(
                page_rgb=page_rgb,
                crop_config=crop_config,
                budget=evidence_budget,
                owner_by_segment=owner_by_segment,
            )
        finally:
            page_rgb.close()

        jobs_by_id = self._validate_queue(
            crop_by_block=crop_by_block,
            budget=evidence_budget,
        )
        self._validate_fusion(
            jobs_by_id=jobs_by_id,
            budget=evidence_budget,
        )
        self._validate_stage4(stage4_config=stage4_config)

    def _validate_types(self) -> None:
        values = (
            ("page", self.page, CropInput),
            ("geometry", self.geometry, GeometryResult),
            ("objects", self.objects, ObjectReconstructionResult),
            ("plan", self.plan, BlockPlan),
            ("queue", self.queue, OcrQueueResult),
            ("fusion", self.fusion, OcrFusionResult),
        )
        for name, value, expected in values:
            if not isinstance(value, expected):
                raise SparsePipelineEvidenceInvariantError(
                    f"{name} must be a {expected.__name__}"
                )
        if type(self.crops) is not tuple or any(
            not isinstance(item, BlockCropPair) for item in self.crops
        ):
            raise SparsePipelineEvidenceInvariantError(
                "crops must be an immutable BlockCropPair tuple"
            )
        if (self.ownership is None) != (self.ownership_segment_ids is None):
            raise SparsePipelineEvidenceInvariantError(
                "ownership raster and segment order must be supplied together"
            )
        if self.ownership is not None and not isinstance(
            self.ownership, np.ndarray
        ):
            raise SparsePipelineEvidenceInvariantError(
                "ownership must be a NumPy array or None"
            )
        if self.ownership_segment_ids is not None and type(
            self.ownership_segment_ids
        ) is not tuple:
            raise SparsePipelineEvidenceInvariantError(
                "ownership segment order must be an immutable tuple or None"
            )
        optional_values = (
            ("stage4", self.stage4, EnhancedCrop),
            ("object_config", self.object_config, ObjectReconstructionConfig),
            ("planning_config", self.planning_config, BlockPlanningConfig),
            ("crop_config", self.crop_config, BlockCropConfig),
            ("fusion_config", self.fusion_config, OcrFusionConfig),
            ("stage4_config", self.stage4_config, CropEnhancementConfig),
        )
        for name, value, expected in optional_values:
            if value is not None and not isinstance(value, expected):
                raise SparsePipelineEvidenceInvariantError(
                    f"{name} must be a {expected.__name__} or None"
                )

    def _validate_limits(
        self,
        *,
        object_config: ObjectReconstructionConfig,
        planning_config: BlockPlanningConfig,
        crop_config: BlockCropConfig,
        fusion_config: OcrFusionConfig,
        stage4_config: CropEnhancementConfig,
    ) -> None:
        segment_count = len(self.geometry.segmentation.segments)
        if segment_count > object_config.max_segments:
            raise SparsePipelineEvidenceInvariantError(
                "segment count exceeds the recorded object configuration"
            )
        if segment_count > planning_config.max_segments:
            raise SparsePipelineEvidenceInvariantError(
                "segment count exceeds the recorded planning configuration"
            )
        if len(self.objects.objects) > planning_config.max_objects:
            raise SparsePipelineEvidenceInvariantError(
                "object count exceeds the recorded planning configuration"
            )
        if len(self.plan.blocks) > min(
            planning_config.max_blocks,
            crop_config.max_blocks,
        ):
            raise SparsePipelineEvidenceInvariantError(
                "block count exceeds a recorded stage configuration"
            )
        if (
            len(self.fusion.observations)
            + len(self.fusion.group_observations)
            > fusion_config.max_observations
        ):
            raise SparsePipelineEvidenceInvariantError(
                "observation count exceeds the recorded fusion configuration"
            )
        if len(self.page.png_bytes) > crop_config.max_page_bytes:
            raise SparsePipelineEvidenceInvariantError(
                "page bytes exceed the recorded crop configuration"
            )
        width, height = self.geometry.segmentation.aligned_size
        if (
            width > crop_config.max_dimension
            or height > crop_config.max_dimension
            or width * height > crop_config.max_page_pixels
        ):
            raise SparsePipelineEvidenceInvariantError(
                "page dimensions exceed the recorded crop configuration"
            )
        if self.stage4 is not None and (
            len(self.page.png_bytes) > stage4_config.max_input_bytes
            or width > stage4_config.max_dimension
            or height > stage4_config.max_dimension
            or width * height > stage4_config.max_input_pixels
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 4 input exceeds its recorded configuration"
            )

    def _validate_objects(self, *, segments: tuple[object, ...]) -> dict[str, str]:
        segment_by_id = {
            getattr(item, "segment_id"): item for item in segments
        }
        owner_by_segment: dict[str, str] = {}
        for item in self.objects.objects:
            try:
                members = tuple(segment_by_id[value] for value in item.segment_ids)
            except KeyError as exc:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 6 object contains a forged segment identifier"
                ) from exc
            if Box.union(member.bbox for member in members) != item.bbox:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 6 object {item.object_id} bbox disagrees with stage 1"
                )
            for segment_id in item.segment_ids:
                if segment_id in owner_by_segment:
                    raise SparsePipelineEvidenceInvariantError(
                        "stage 6 assigns one segment to multiple objects"
                    )
                owner_by_segment[segment_id] = item.object_id
        if set(owner_by_segment) != set(self.objects.source_segment_ids):
            raise SparsePipelineEvidenceInvariantError(
                "stage 6 ownership is not the exact stage 1 segment partition"
            )
        ownership_records = {
            item.segment_id: item.object_id
            for item in self.objects.segment_ownership
        }
        if ownership_records != owner_by_segment:
            raise SparsePipelineEvidenceInvariantError(
                "stage 6 ownership records disagree with its objects"
            )
        return owner_by_segment

    def _validate_plan(
        self,
        *,
        segments: tuple[object, ...],
        owner_by_segment: dict[str, str],
        planning_config: BlockPlanningConfig,
        matrix: SparseSegmentMatrix,
        budget: _EvidenceBudget | None = None,
    ) -> None:
        budget = budget or _EvidenceBudget(
            planning_config.max_evidence_revalidation_checks
        )
        segment_by_id = {
            getattr(item, "segment_id"): item for item in segments
        }
        if self.plan.mode is not planning_config.mode:
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 plan mode disagrees with its recorded configuration"
            )
        if self.plan.mode is BlockPlanningMode.SPATIAL_2D and (
            self.plan.matrix_sha256 != sparse_matrix_sha256(matrix)
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 matrix provenance disagrees with Stage 1"
            )
        width, height = self.plan.aligned_size
        memberships = {segment_id: 0 for segment_id in segment_by_id}
        budget.consume(len(segments) + len(self.plan.blocks))
        scope_owner: dict[str, str] = {}
        owner_scope: dict[str, str] = {}
        for block in self.plan.blocks:
            budget.consume(
                len(block.segment_ids)
                + len(block.core_segment_ids)
                + len(block.object_ids)
            )
            if (
                (
                    planning_config.mode is BlockPlanningMode.FULL_WIDTH
                    and len(block.core_segment_ids)
                    > planning_config.max_core_segments
                )
                or (
                    planning_config.mode is BlockPlanningMode.SPATIAL_2D
                    and len(block.core_segment_ids)
                    > planning_config.max_block_segments
                )
                or len(block.segment_ids) > planning_config.max_block_segments
            ):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 5 block {block.block_id} exceeds segment limits"
                )
            for segment_id in block.segment_ids:
                memberships[segment_id] += 1
            expected_owners = tuple(
                dict.fromkeys(
                    owner_by_segment[segment_id]
                    for segment_id in block.core_segment_ids
                )
            )
            if block.object_ids != expected_owners:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 5 block {block.block_id} object identifiers disagree"
                )
            if planning_config.mode is BlockPlanningMode.SPATIAL_2D:
                block_owners = {
                    owner_by_segment[segment_id]
                    for segment_id in block.segment_ids
                }
                if block.scope_id is None or len(block_owners) != 1:
                    raise SparsePipelineEvidenceInvariantError(
                        "every spatial block must belong to one Stage 6 object"
                    )
                owner = next(iter(block_owners))
                if scope_owner.setdefault(block.scope_id, owner) != owner:
                    raise SparsePipelineEvidenceInvariantError(
                        "one spatial scope crosses Stage 6 objects"
                    )
                if owner_scope.setdefault(owner, block.scope_id) != block.scope_id:
                    raise SparsePipelineEvidenceInvariantError(
                        "one Stage 6 object has multiple spatial scopes"
                    )
                if any(
                    owner_by_segment[segment_id] != owner
                    for segment_id in block.segment_ids
                ):
                    raise SparsePipelineEvidenceInvariantError(
                        "one spatial block crosses Stage 6 objects"
                    )
            member_bbox = Box.union(
                segment_by_id[segment_id].bbox
                for segment_id in block.segment_ids
            )
            if planning_config.mode is BlockPlanningMode.FULL_WIDTH:
                expected_bbox = Box(
                    0,
                    max(0, member_bbox.top - planning_config.padding),
                    width,
                    min(height, member_bbox.bottom + planning_config.padding),
                )
            else:
                member_ids = set(block.segment_ids)
                budget.consume(len(self.plan.source_segment_ids))
                excluded = tuple(
                    segment_by_id[segment_id]
                    for segment_id in self.plan.source_segment_ids
                    if segment_id not in member_ids
                )
                # Different Stage 6 objects can have overlapping conservative
                # bboxes while their literal Stage 1 ownership pixels remain
                # disjoint.  The planner deliberately permits that geometry
                # and BlockCropper removes any physically visible foreign
                # ownership before OCR.  Do not reintroduce the obsolete
                # bbox-only fixed-point rule here: __post_init__ independently
                # recomputes every masked ID and pixel from the exact ownership
                # raster before crop/page/queue evidence is accepted.
                try:
                    expected_bbox = _clamped_spatial_bbox(
                        member_bbox,
                        excluded_segments=excluded,
                        aligned_size=(width, height),
                        padding=planning_config.padding,
                        work_budget=budget,
                    )
                except BlockPlanningInvariantError as exc:
                    raise SparsePipelineEvidenceInvariantError(
                        f"stage 5 block {block.block_id} spatial padding "
                        "disagrees with stage 1"
                    ) from exc
            if block.bbox != expected_bbox:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 5 block {block.block_id} bbox disagrees with stage 1"
                )
            if block.bbox.area > planning_config.max_block_pixels:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 5 block {block.block_id} pixel footprint exceeds "
                    "the recorded limit"
                )
        if (
            max(memberships.values(), default=0)
            > planning_config.max_segment_memberships
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 segment memberships exceed the recorded limit"
            )
        if (
            sum(len(block.segment_ids) for block in self.plan.blocks)
            > planning_config.max_total_block_memberships
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 aggregate memberships exceed the recorded limit"
            )
        if any(
            not block.core_segment_ids and len(block.segment_ids) == 1
            for block in self.plan.blocks
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 singleton signature probes are forbidden"
            )
        if len(self.plan.adjacent_algebra) > planning_config.max_overlap_pairs:
            raise SparsePipelineEvidenceInvariantError(
                "stage 5 overlap pairs exceed the recorded limit"
            )
        pair_memberships = sum(
            len(item.union_segment_ids)
            for item in self.plan.adjacent_algebra
        )
        if pair_memberships > planning_config.max_pair_memberships:
            raise SparsePipelineEvidenceInvariantError(
                    "stage 5 block pair memberships exceed the recorded limit"
                )
        for unit in self.plan.membership_units:
            budget.consume(len(unit.segment_ids) + len(unit.block_ids))
            owners = {owner_by_segment[value] for value in unit.segment_ids}
            if len(owners) != 1:
                raise SparsePipelineEvidenceInvariantError(
                    "one membership unit crosses Stage 6 objects"
                )

    def _validate_crops(
        self,
        *,
        page_rgb: Image.Image,
        crop_config: BlockCropConfig,
        budget: _EvidenceBudget,
        owner_by_segment: dict[str, str],
    ) -> dict[str, BlockCropPair]:
        if tuple(item.block_id for item in self.crops) != tuple(
            item.block_id for item in self.plan.blocks
        ):
            raise SparsePipelineEvidenceInvariantError(
                "stage 4 crops do not follow the exact stage 5 block order"
            )
        crop_by_block: dict[str, BlockCropPair] = {}
        for block, crop in zip(self.plan.blocks, self.crops):
            budget.consume(1)
            if (
                crop.bbox != block.bbox
                or crop.segment_ids != block.segment_ids
            ):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 4 crop {crop.block_id} disagrees with stage 5"
                )
            if any(
                segment_id not in owner_by_segment
                or segment_id in block.segment_ids
                for segment_id in crop.masked_segment_ids
            ):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 4 crop {crop.block_id} has an invalid isolation scope"
                )
            if crop.gamma.dpi != crop_config.dpi:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 4 crop {crop.block_id} DPI disagrees with its config"
                )
            if (
                crop_config.enhancement_backend is not EnhancementBackend.AUTO
                and crop.gamma.backend is not crop_config.enhancement_backend
            ):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 4 crop {crop.block_id} backend disagrees with its config"
                )
            expected = page_rgb.crop(block.bbox.as_tuple())
            if crop.isolation_mask_png is not None:
                try:
                    with Image.open(
                        io.BytesIO(crop.isolation_mask_png)
                    ) as mask:
                        mask.load()
                        expected.paste((255, 255, 255), mask=mask)
                except (OSError, UnidentifiedImageError, SyntaxError) as exc:
                    expected.close()
                    raise SparsePipelineEvidenceInvariantError(
                        f"isolation mask {crop.block_id} is invalid"
                    ) from exc
            actual = self._decode_rgb(
                crop.raw.png_bytes,
                expected_size=(block.bbox.width, block.bbox.height),
                label=f"raw crop {crop.block_id}",
            )
            try:
                if self._rgb_sha256(expected) != self._rgb_sha256(actual):
                    raise SparsePipelineEvidenceInvariantError(
                        f"stage 4 crop {crop.block_id} pixels disagree with the page"
                    )
            finally:
                expected.close()
                actual.close()
            crop_by_block[crop.block_id] = crop
        return crop_by_block

    def _validate_queue(
        self,
        *,
        crop_by_block: dict[str, BlockCropPair],
        budget: _EvidenceBudget,
    ) -> dict[str, OcrJobResult]:
        jobs_by_id: dict[str, OcrJobResult] = {}
        for job in self.queue.jobs:
            budget.consume(1)
            if job.job_id in jobs_by_id:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 queue job identifiers must be unique"
                )
            crop = crop_by_block.get(job.block_id)
            if crop is None:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 job {job.job_id} references an unknown block"
                )
            payload = (
                crop.raw.png_bytes
                if job.transform in (
                    OcrTransform.RAW,
                    OcrTransform.CONTEXTUAL_COMPOSITE,
                )
                else crop.gamma.png_bytes
            )
            expected_input = hashlib.sha256(payload).hexdigest()
            expected_context = hashlib.sha256(crop.raw.png_bytes).hexdigest()
            if job.input_sha256 != expected_input:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 job {job.job_id} input digest disagrees with its crop"
                )
            if job.context_sha256 != expected_context:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 job {job.job_id} context digest disagrees with its block"
                )
            jobs_by_id[job.job_id] = job
        return jobs_by_id

    def _validate_fusion(
        self,
        *,
        jobs_by_id: dict[str, OcrJobResult],
        budget: _EvidenceBudget,
    ) -> None:
        observation_by_id = {}
        evidence = (
            *self.fusion.observations,
            *self.fusion.group_observations,
            *self.fusion.block_text_observations,
            *self.fusion.unassigned_word_observations,
        )
        for item in evidence:
            budget.consume(1)
            job = jobs_by_id.get(item.job_id)
            if job is None:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 fusion references an unknown queue job"
                )
            actual = (
                item.block_id,
                item.transform,
                item.lane_id,
                item.capability_id,
                item.input_sha256,
                item.context_sha256,
            )
            expected = (
                job.block_id,
                job.transform,
                job.lane_id,
                job.capability_id,
                job.input_sha256,
                job.context_sha256,
            )
            if actual != expected:
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 fusion evidence for {item.job_id} disagrees with its job"
                )
            observation_id = getattr(item, "observation_id", None)
            if observation_id is not None:
                if observation_id in observation_by_id:
                    raise SparsePipelineEvidenceInvariantError(
                        "stage 2 observation identifiers must be unique"
                    )
                observation_by_id[observation_id] = item

        unit_by_id = {item.unit_id: item for item in self.plan.membership_units}
        group_observations_by_unit: dict[str, list[object]] = {}
        for item in self.fusion.group_observations:
            budget.consume(1)
            unit = unit_by_id.get(item.unit_id)
            if (
                unit is None
                or item.segment_ids != unit.segment_ids
                or item.block_id not in unit.block_ids
            ):
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 group observation disagrees with Stage 5 membership"
                )
            group_observations_by_unit.setdefault(item.unit_id, []).append(item)
        for item in self.fusion.segment_groups:
            budget.consume(1)
            unit = unit_by_id.get(item.unit_id)
            observed = group_observations_by_unit.get(item.unit_id, ())
            if (
                unit is None
                or item.segment_ids != unit.segment_ids
                or item.observation_count != len(observed)
            ):
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 group fusion disagrees with Stage 5 membership"
                )

        observations_by_segment: dict[str, list[object]] = {
            segment_id: [] for segment_id in self.fusion.source_segment_ids
        }
        for item in self.fusion.observations:
            budget.consume(1)
            try:
                observations_by_segment[item.segment_id].append(item)
            except KeyError as exc:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 observation references an unknown segment"
                ) from exc
        for item in self.fusion.segments:
            budget.consume(1)
            if item.observation_count != len(observations_by_segment[item.segment_id]):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 fusion count for {item.segment_id} disagrees"
                )
            if item.selected_observation_id is None:
                continue
            selected = observation_by_id.get(item.selected_observation_id)
            if selected is None or (
                selected.segment_id != item.segment_id
                or selected.text != item.selected_text
                or selected.transform is not item.selected_transform
                or selected.lane_id != item.selected_lane_id
            ):
                raise SparsePipelineEvidenceInvariantError(
                    f"stage 2 selection for {item.segment_id} is forged"
                )

        if len(self.fusion.overlaps) != len(self.plan.adjacent_algebra):
            raise SparsePipelineEvidenceInvariantError(
                "stage 2 overlap count disagrees with stage 5"
            )
        for algebra, overlap in zip(
            self.plan.adjacent_algebra,
            self.fusion.overlaps,
        ):
            budget.consume(1)
            if (
                overlap.first_block_id,
                overlap.second_block_id,
                overlap.intersection_segment_ids,
                overlap.union_segment_ids,
                overlap.xor_segment_ids,
            ) != (
                algebra.first_block_id,
                algebra.second_block_id,
                algebra.intersection_segment_ids,
                algebra.union_segment_ids,
                algebra.xor_segment_ids,
            ):
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 overlap algebra disagrees with stage 5"
                )

        for conflict in self.fusion.replica_conflicts:
            budget.consume(1 + len(conflict.job_ids))
            try:
                jobs = tuple(jobs_by_id[job_id] for job_id in conflict.job_ids)
            except KeyError as exc:
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 replica conflict references an unknown job"
                ) from exc
            if any(
                (
                    item.context_sha256,
                    item.capability_id,
                    item.transform,
                )
                != (
                    conflict.context_sha256,
                    conflict.capability_id,
                    conflict.transform,
                )
                for item in jobs
            ):
                raise SparsePipelineEvidenceInvariantError(
                    "stage 2 replica conflict provenance disagrees with its jobs"
                )

    def _validate_stage4(self, *, stage4_config: CropEnhancementConfig) -> None:
        if self.stage4 is None:
            return
        aligned_size = self.geometry.segmentation.aligned_size
        if (self.stage4.width, self.stage4.height) != aligned_size:
            raise SparsePipelineEvidenceInvariantError(
                "optional stage 4 candidate size disagrees with stage 1"
            )
        if self.stage4.source_sha256 != hashlib.sha256(
            self.page.png_bytes
        ).hexdigest():
            raise SparsePipelineEvidenceInvariantError(
                "optional stage 4 candidate was not derived from the page"
            )
        if self.stage4.dpi != stage4_config.dpi:
            raise SparsePipelineEvidenceInvariantError(
                "optional stage 4 candidate DPI disagrees with its config"
            )
        if (
            stage4_config.backend is not EnhancementBackend.AUTO
            and self.stage4.backend is not stage4_config.backend
        ):
            raise SparsePipelineEvidenceInvariantError(
                "optional stage 4 candidate backend disagrees with its config"
            )

    @staticmethod
    def _decode_rgb(
        payload: bytes,
        *,
        expected_size: tuple[int, int],
        label: str,
    ) -> Image.Image:
        if not payload.startswith(PNG_SIGNATURE):
            raise SparsePipelineEvidenceInvariantError(f"{label} is not a PNG")
        try:
            with Image.open(io.BytesIO(payload)) as opened:
                if (
                    opened.format != "PNG"
                    or opened.size != expected_size
                    or getattr(opened, "n_frames", 1) != 1
                ):
                    raise SparsePipelineEvidenceInvariantError(
                        f"{label} metadata disagrees with its stage"
                    )
                raw_exif = opened.info.get("exif")
                if raw_exif is None:
                    orientation = 1
                elif type(raw_exif) is not bytes:
                    raise SparsePipelineEvidenceInvariantError(
                        f"{label} contains invalid EXIF metadata"
                    )
                else:
                    metadata = Image.Exif()
                    metadata.load(raw_exif)
                    orientation = metadata.get(274, 1)
                if orientation not in (None, 1):
                    raise SparsePipelineEvidenceInvariantError(
                        f"{label} contains unresolved orientation"
                    )
                opened.load()
                if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
                    rgba = opened.convert("RGBA")
                    canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    try:
                        canvas.alpha_composite(rgba)
                        return canvas.convert("RGB")
                    finally:
                        rgba.close()
                        canvas.close()
                return opened.convert("RGB")
        except SparsePipelineEvidenceInvariantError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
            raise SparsePipelineEvidenceInvariantError(
                f"{label} contains invalid PNG data"
            ) from exc

    @staticmethod
    def _rgb_sha256(image: Image.Image) -> str:
        digest = hashlib.sha256()
        for top in range(0, image.height, 512):
            stripe = image.crop(
                (0, top, image.width, min(image.height, top + 512))
            )
            try:
                digest.update(stripe.tobytes())
            finally:
                stripe.close()
        return digest.hexdigest()


__all__ = [
    "SparsePipelineEvidence",
    "SparsePipelineEvidenceInvariantError",
]
