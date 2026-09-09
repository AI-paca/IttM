from __future__ import annotations

import hashlib
import heapq
import json
import math
import unicodedata
from dataclasses import dataclass
from enum import Enum

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    MembershipUnit,
    MembershipUnitKind,
)
from app.sparse_pipeline.contracts import Box, Segment
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrAttributionStatus,
    OcrOutputGeometry,
)
from app.sparse_pipeline.ocr_queue import (
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrTransform,
    OcrWord,
)


class OcrFusionInvariantError(ValueError):
    """Raised when geometry, queue output and routed evidence disagree."""


class OcrFusionLimitError(RuntimeError):
    """Raised before exact evidence fusion exceeds an explicit work bound."""


class EditKind(str, Enum):
    MATCH = "match"
    SUBSTITUTE = "substitute"
    DELETE = "delete"
    INSERT = "insert"


class OcrFusionStatus(str, Enum):
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"


class OcrRoutingMode(str, Enum):
    """How observed OCR words are attributed to Stage 1 segments.

    ``BBOX_INTERSECTION`` preserves the original A/B control: every projected
    OCR word is compared directly with the member segment boxes of its block.

    ``BLOCK_MEMBERSHIP`` never compares an OCR word with a segment box.  It
    correlates the same observed page geometry across block crops, turns the
    distinct block IDs into a membership signature, and accepts the result
    only when that exact signature identifies one source segment.
    """

    BBOX_INTERSECTION = "bbox_intersection"
    BLOCK_MEMBERSHIP = "block_membership"


@dataclass(frozen=True)
class OcrFusionConfig:
    max_segments: int = 100_000
    max_jobs: int = 100_000
    # Fusion may validate archived/replicated evidence produced outside the
    # live queue (whose own production default is stricter).  Keep this bound
    # high enough for stress evidence while still preventing unbounded lane
    # discovery and matrix multiplication.
    max_lanes: int = 50_000
    max_observations: int = 2_000_000
    max_text_chars: int = 256_000
    max_alignment_cells: int = 4_000_000
    max_total_alignment_cells: int = 64_000_000
    max_pairwise_alignments: int = 1_000_000
    max_routing_comparisons: int = 20_000_000
    max_membership_geometry_comparisons: int = 20_000_000
    max_membership_sweep_checks: int = 20_000_000
    max_reading_order_checks: int = 20_000_000
    max_membership_block_assignments: int = 2_000_000
    max_membership_signature_comparisons: int = 20_000_000
    minimum_stability: float = 0.75
    minimum_confidence: float = 0.5
    minimum_exact_context_majority_fraction: float = 0.8
    minimum_alternative_contexts: int = 2
    minimum_significant_overlap_fraction: float = 0.1
    minimum_membership_bbox_iou: float = 0.5
    maximum_membership_center_distance_fraction: float = 0.5
    membership_assume_complete_observations: bool = False
    require_exact_job_matrix: bool = True
    routing_mode: OcrRoutingMode = OcrRoutingMode.BBOX_INTERSECTION

    def __post_init__(self) -> None:
        limits = (
            self.max_segments,
            self.max_jobs,
            self.max_lanes,
            self.max_observations,
            self.max_text_chars,
            self.max_alignment_cells,
            self.max_total_alignment_cells,
            self.max_pairwise_alignments,
            self.max_routing_comparisons,
            self.max_membership_geometry_comparisons,
            self.max_membership_sweep_checks,
            self.max_reading_order_checks,
            self.max_membership_block_assignments,
            self.max_membership_signature_comparisons,
            self.minimum_alternative_contexts,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("OCR fusion limits must be positive integers")
        for name, value in (
            ("minimum_stability", self.minimum_stability),
            ("minimum_confidence", self.minimum_confidence),
            (
                "minimum_exact_context_majority_fraction",
                self.minimum_exact_context_majority_fraction,
            ),
            (
                "minimum_significant_overlap_fraction",
                self.minimum_significant_overlap_fraction,
            ),
            ("minimum_membership_bbox_iou", self.minimum_membership_bbox_iou),
            (
                "maximum_membership_center_distance_fraction",
                self.maximum_membership_center_distance_fraction,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be between zero and one")
        if self.minimum_exact_context_majority_fraction <= 0.5:
            raise ValueError("minimum_exact_context_majority_fraction must be greater than one half")
        if not isinstance(self.routing_mode, OcrRoutingMode):
            raise ValueError("routing_mode must be an OcrRoutingMode")
        if type(self.membership_assume_complete_observations) is not bool:
            raise ValueError("membership_assume_complete_observations must be a boolean")
        if type(self.require_exact_job_matrix) is not bool:
            raise ValueError("require_exact_job_matrix must be a boolean")
        if self.membership_assume_complete_observations and not self.require_exact_job_matrix:
            raise ValueError("complete membership observations require an exact job matrix")


@dataclass(frozen=True)
class EditOperation:
    kind: EditKind
    left_index: int | None
    right_index: int | None
    left_character: str | None
    right_character: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EditKind):
            raise ValueError("edit operation kind is invalid")
        if self.kind in {EditKind.MATCH, EditKind.SUBSTITUTE}:
            if (
                type(self.left_index) is not int
                or type(self.right_index) is not int
                or type(self.left_character) is not str
                or len(self.left_character) != 1
                or type(self.right_character) is not str
                or len(self.right_character) != 1
            ):
                raise ValueError("paired edit operation needs both codepoints")
        elif self.kind is EditKind.DELETE:
            if (
                type(self.left_index) is not int
                or self.right_index is not None
                or type(self.left_character) is not str
                or len(self.left_character) != 1
                or self.right_character is not None
            ):
                raise ValueError("delete operation has invalid coordinates")
        elif (
            self.left_index is not None
            or type(self.right_index) is not int
            or self.left_character is not None
            or type(self.right_character) is not str
            or len(self.right_character) != 1
        ):
            raise ValueError("insert operation has invalid coordinates")


@dataclass(frozen=True)
class ExactTextAlignment:
    left: str
    right: str
    operations: tuple[EditOperation, ...]
    distance: int
    similarity: float

    def __post_init__(self) -> None:
        if type(self.left) is not str or type(self.right) is not str:
            raise ValueError("alignment inputs must be strings")
        if type(self.operations) is not tuple or any(not isinstance(item, EditOperation) for item in self.operations):
            raise ValueError("alignment operations must be immutable")
        if type(self.distance) is not int or self.distance < 0:
            raise ValueError("alignment distance must be non-negative")
        if not 0.0 <= self.similarity <= 1.0:
            raise ValueError("alignment similarity must be between zero and one")


@dataclass(frozen=True)
class SegmentObservation:
    observation_id: str
    job_id: str
    segment_id: str
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    text: str
    confidence: float
    page_bboxes: tuple[Box, ...]
    input_sha256: str
    context_sha256: str
    source_replica_conflict: bool

    def __post_init__(self) -> None:
        identifiers = (
            self.observation_id,
            self.job_id,
            self.segment_id,
            self.block_id,
            self.lane_id,
            self.capability_id,
        )
        if any(type(value) is not str or not value for value in identifiers):
            raise ValueError("segment observation identifiers must not be empty")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("segment observation transform is invalid")
        if type(self.text) is not str:
            raise ValueError("segment observation text must be observed verbatim")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("segment observation confidence is invalid")
        if type(self.page_bboxes) is not tuple or any(not isinstance(item, Box) for item in self.page_bboxes):
            raise ValueError("segment observation boxes must be immutable")
        for name, value in (
            ("input_sha256", self.input_sha256),
            ("context_sha256", self.context_sha256),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"segment observation {name} is invalid")
        if type(self.source_replica_conflict) is not bool:
            raise ValueError("segment observation replica-conflict flag is invalid")

    @property
    def comparison_text(self) -> str:
        return compact_ocr_text(self.text)


@dataclass(frozen=True)
class SegmentGroupObservation:
    observation_id: str
    job_id: str
    unit_id: str
    segment_ids: tuple[str, ...]
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    text: str
    confidence: float
    page_bboxes: tuple[Box, ...]
    input_sha256: str
    context_sha256: str
    source_replica_conflict: bool

    def __post_init__(self) -> None:
        if type(self.unit_id) is not str or not self.unit_id.startswith("membership-unit-"):
            raise ValueError("group observation membership unit ID is invalid")
        if (
            type(self.segment_ids) is not tuple
            or len(self.segment_ids) < 2
            or len(self.segment_ids) != len(set(self.segment_ids))
            or any(type(item) is not str or not item for item in self.segment_ids)
        ):
            raise ValueError("group observation needs an immutable subblock")
        # Reuse the mature observation validation without claiming that the
        # group is one source segment.
        SegmentObservation(
            observation_id=self.observation_id,
            job_id=self.job_id,
            segment_id=self.unit_id,
            block_id=self.block_id,
            transform=self.transform,
            lane_id=self.lane_id,
            capability_id=self.capability_id,
            text=self.text,
            confidence=self.confidence,
            page_bboxes=self.page_bboxes,
            input_sha256=self.input_sha256,
            context_sha256=self.context_sha256,
            source_replica_conflict=self.source_replica_conflict,
        )

    @property
    def comparison_text(self) -> str:
        return compact_ocr_text(self.text)


@dataclass(frozen=True)
class ObservationAlignment:
    observation_id: str
    alignment: ExactTextAlignment


@dataclass(frozen=True)
class BlockTextObservation:
    job_id: str
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    text: str
    attribution_status: OcrAttributionStatus
    attribution_reason: str
    input_sha256: str
    context_sha256: str
    source_replica_conflict: bool

    def __post_init__(self) -> None:
        for value in (
            self.job_id,
            self.block_id,
            self.lane_id,
            self.capability_id,
        ):
            if type(value) is not str or not value:
                raise ValueError("block text observation identifiers must not be empty")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("block text observation transform is invalid")
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("block text observation must contain observed text")
        if self.attribution_status is not OcrAttributionStatus.UNATTRIBUTABLE:
            raise ValueError("text-only block output must remain unattributable")
        if self.attribution_reason != "no-observed-bbox":
            raise ValueError("text-only attribution reason is invalid")
        for value in (self.input_sha256, self.context_sha256):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("block text observation digest is invalid")
        if type(self.source_replica_conflict) is not bool:
            raise ValueError("block text replica-conflict flag is invalid")


@dataclass(frozen=True)
class UnassignedWordObservation:
    job_id: str
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    word_index: int
    text: str
    confidence: float
    page_bbox: Box
    input_sha256: str
    context_sha256: str
    reason: str

    def __post_init__(self) -> None:
        for value in (
            self.job_id,
            self.block_id,
            self.lane_id,
            self.capability_id,
            self.text,
        ):
            if type(value) is not str or not value:
                raise ValueError("unassigned OCR word identifiers/text must not be empty")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("unassigned OCR word transform is invalid")
        if type(self.word_index) is not int or self.word_index < 0:
            raise ValueError("unassigned OCR word index is invalid")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("unassigned OCR word confidence is invalid")
        if not isinstance(self.page_bbox, Box):
            raise ValueError("unassigned OCR word bbox is invalid")
        for value in (self.input_sha256, self.context_sha256):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("unassigned OCR word digest is invalid")
        if self.reason not in {
            "no-segment-intersection",
            "ambiguous-segment-intersection",
            "membership-geometry-ambiguous",
            "membership-signature-unmatched",
            "membership-signature-ambiguous",
            "membership-signature-omission-ambiguous",
            "membership-observation-lattice-incomplete",
        }:
            raise ValueError("unassigned OCR word reason is invalid")


@dataclass(frozen=True)
class _ProjectedWordObservation:
    """One immutable OCR word projected from its crop onto the page."""

    ordinal: int
    job: OcrJobResult
    word_index: int
    word: OcrWord
    page_bbox: Box
    source_replica_conflict: bool


@dataclass(frozen=True)
class OcrReplicaConflict:
    context_sha256: str
    capability_id: str
    transform: OcrTransform
    job_ids: tuple[str, ...]
    evidence_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.context_sha256) is not str
            or len(self.context_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.context_sha256)
        ):
            raise ValueError("OCR replica conflict context digest is invalid")
        if type(self.capability_id) is not str or not self.capability_id:
            raise ValueError("OCR replica conflict capability is invalid")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("OCR replica conflict transform is invalid")
        if (
            type(self.job_ids) is not tuple
            or len(self.job_ids) < 2
            or len(self.job_ids) != len(set(self.job_ids))
            or any(type(value) is not str or not value for value in self.job_ids)
        ):
            raise ValueError("OCR replica conflict jobs are invalid")
        if (
            type(self.evidence_sha256) is not tuple
            or len(self.evidence_sha256) < 2
            or len(self.evidence_sha256) != len(set(self.evidence_sha256))
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in self.evidence_sha256
            )
        ):
            raise ValueError("OCR replica conflict evidence digests are invalid")


@dataclass(frozen=True)
class SegmentFusion:
    segment_id: str
    selected_text: str | None
    selected_observation_id: str | None
    selected_transform: OcrTransform | None
    selected_lane_id: str | None
    confidence: float | None
    observation_count: int
    independent_context_count: int
    stability: float
    uncertainty_reasons: tuple[str, ...]
    script_scores: tuple[tuple[str, float], ...]
    alignments: tuple[ObservationAlignment, ...]

    def __post_init__(self) -> None:
        if type(self.segment_id) is not str or not self.segment_id:
            raise ValueError("segment fusion identifier must not be empty")
        selected = self.selected_text is not None
        selected_fields = (
            self.selected_observation_id,
            self.selected_transform,
            self.selected_lane_id,
            self.confidence,
        )
        if selected != all(value is not None for value in selected_fields):
            raise ValueError("selected segment evidence fields disagree")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("selected segment confidence is invalid")
        if min(self.observation_count, self.independent_context_count) < 0:
            raise ValueError("segment fusion counts must be non-negative")
        if not 0.0 <= self.stability <= 1.0:
            raise ValueError("segment stability must be between zero and one")

    @property
    def unresolved(self) -> bool:
        blocking = {
            "capability_replica_conflict",
            "low_confidence",
            "transform_conflict",
            "unstable_raw_text",
        }
        return self.selected_text is None or bool(blocking.intersection(self.uncertainty_reasons))


@dataclass(frozen=True)
class SegmentGroupFusion:
    unit_id: str
    segment_ids: tuple[str, ...]
    selected_text: str | None
    selected_observation_id: str | None
    selected_transform: OcrTransform | None
    selected_lane_id: str | None
    confidence: float | None
    observation_count: int
    independent_context_count: int
    stability: float
    uncertainty_reasons: tuple[str, ...]
    script_scores: tuple[tuple[str, float], ...]
    alignments: tuple[ObservationAlignment, ...]

    def __post_init__(self) -> None:
        if (
            type(self.unit_id) is not str
            or not self.unit_id.startswith("membership-unit-")
            or type(self.segment_ids) is not tuple
            or len(self.segment_ids) < 2
            or len(self.segment_ids) != len(set(self.segment_ids))
        ):
            raise ValueError("segment group fusion identity is invalid")
        SegmentFusion(
            segment_id=self.unit_id,
            selected_text=self.selected_text,
            selected_observation_id=self.selected_observation_id,
            selected_transform=self.selected_transform,
            selected_lane_id=self.selected_lane_id,
            confidence=self.confidence,
            observation_count=self.observation_count,
            independent_context_count=self.independent_context_count,
            stability=self.stability,
            uncertainty_reasons=self.uncertainty_reasons,
            script_scores=self.script_scores,
            alignments=self.alignments,
        )

    @property
    def unresolved(self) -> bool:
        blocking = {
            "capability_replica_conflict",
            "low_confidence",
            "transform_conflict",
            "unstable_raw_text",
        }
        return self.selected_text is None or bool(blocking.intersection(self.uncertainty_reasons))


@dataclass(frozen=True)
class OverlapConsensus:
    first_block_id: str
    second_block_id: str
    intersection_segment_ids: tuple[str, ...]
    union_segment_ids: tuple[str, ...]
    xor_segment_ids: tuple[str, ...]
    confirmed_intersection_segment_ids: tuple[str, ...]
    cross_transform_confirmed_intersection_segment_ids: tuple[str, ...]
    near_confirmed_intersection_segment_ids: tuple[str, ...]
    deferred_intersection_segment_ids: tuple[str, ...]
    conflicting_intersection_segment_ids: tuple[str, ...]
    missing_intersection_segment_ids: tuple[str, ...]
    observed_union_segment_ids: tuple[str, ...]
    observed_xor_segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.first_block_id) is not str or not self.first_block_id:
            raise ValueError("overlap first block identifier must not be empty")
        if type(self.second_block_id) is not str or not self.second_block_id:
            raise ValueError("overlap second block identifier must not be empty")
        if self.first_block_id == self.second_block_id:
            raise ValueError("overlap blocks must be distinct")
        categories = (
            self.confirmed_intersection_segment_ids,
            self.cross_transform_confirmed_intersection_segment_ids,
            self.near_confirmed_intersection_segment_ids,
            self.deferred_intersection_segment_ids,
            self.conflicting_intersection_segment_ids,
            self.missing_intersection_segment_ids,
        )
        sequences = (
            self.intersection_segment_ids,
            self.union_segment_ids,
            self.xor_segment_ids,
            self.observed_union_segment_ids,
            self.observed_xor_segment_ids,
            *categories,
        )
        if any(
            type(values) is not tuple
            or len(values) != len(set(values))
            or any(type(value) is not str or not value for value in values)
            for values in sequences
        ):
            raise ValueError("overlap segment identifiers must be unique tuples")
        intersection = set(self.intersection_segment_ids)
        categorized = [value for values in categories for value in values]
        if len(categorized) != len(set(categorized)):
            raise ValueError("overlap consensus categories must be disjoint")
        if set(categorized) != intersection:
            raise ValueError("overlap consensus must classify every intersection")
        category_sets = tuple(set(values) for values in categories)
        for values, value_set in zip(categories, category_sets):
            if values != tuple(value for value in self.intersection_segment_ids if value in value_set):
                raise ValueError("overlap consensus categories must be canonical")
        union = set(self.union_segment_ids)
        if not intersection.issubset(union):
            raise ValueError("overlap intersection must be inside its union")
        if not set(self.xor_segment_ids).issubset(union):
            raise ValueError("overlap xor must be inside its union")
        if not set(self.observed_union_segment_ids).issubset(union):
            raise ValueError("observed overlap union contains an unknown segment")
        if not set(self.observed_xor_segment_ids).issubset(set(self.xor_segment_ids)):
            raise ValueError("observed overlap xor contains an unknown segment")


@dataclass(frozen=True)
class OcrFusionResult:
    source_segment_ids: tuple[str, ...]
    observations: tuple[SegmentObservation, ...]
    block_text_observations: tuple[BlockTextObservation, ...]
    unassigned_word_observations: tuple[UnassignedWordObservation, ...]
    replica_conflicts: tuple[OcrReplicaConflict, ...]
    segments: tuple[SegmentFusion, ...]
    overlaps: tuple[OverlapConsensus, ...]
    status: OcrFusionStatus
    diagnostics: tuple[str, ...] = ()
    group_observations: tuple[SegmentGroupObservation, ...] = ()
    segment_groups: tuple[SegmentGroupFusion, ...] = ()

    def __post_init__(self) -> None:
        if type(self.source_segment_ids) is not tuple:
            raise ValueError("fusion source IDs must be immutable")
        if type(self.group_observations) is not tuple or any(
            not isinstance(item, SegmentGroupObservation) for item in self.group_observations
        ):
            raise ValueError("group observations must be immutable")
        if type(self.segment_groups) is not tuple or any(
            not isinstance(item, SegmentGroupFusion) for item in self.segment_groups
        ):
            raise ValueError("segment group fusions must be immutable")
        group_ids = tuple(item.unit_id for item in self.segment_groups)
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("segment group fusion IDs must be unique")
        grouped_segment_ids = tuple(segment_id for item in self.segment_groups for segment_id in item.segment_ids)
        if len(grouped_segment_ids) != len(set(grouped_segment_ids)) or not set(grouped_segment_ids).issubset(
            self.source_segment_ids
        ):
            raise ValueError("segment group fusions must be a source partition")
        group_observation_by_id = {item.observation_id: item for item in self.group_observations}
        if len(group_observation_by_id) != len(self.group_observations):
            raise ValueError("group observation IDs must be unique")
        group_observations_by_unit: dict[str, list[SegmentGroupObservation]] = {}
        for observation in self.group_observations:
            group_observations_by_unit.setdefault(observation.unit_id, []).append(observation)
        for item in self.segment_groups:
            observed = tuple(group_observations_by_unit.get(item.unit_id, ()))
            if item.observation_count != len(observed):
                raise ValueError("segment group observation count disagrees")
            if item.selected_observation_id is not None:
                selected = group_observation_by_id.get(item.selected_observation_id)
                if (
                    selected is None
                    or selected.unit_id != item.unit_id
                    or selected.segment_ids != item.segment_ids
                    or selected.text != item.selected_text
                ):
                    raise ValueError("segment group selection is forged")
        if tuple(item.segment_id for item in self.segments) != self.source_segment_ids:
            raise ValueError("segment fusions must follow canonical source order")
        if type(self.block_text_observations) is not tuple or any(
            not isinstance(item, BlockTextObservation) for item in self.block_text_observations
        ):
            raise ValueError("block text observations must be immutable")
        if type(self.unassigned_word_observations) is not tuple or any(
            not isinstance(item, UnassignedWordObservation) for item in self.unassigned_word_observations
        ):
            raise ValueError("unassigned OCR word observations must be immutable")
        if type(self.replica_conflicts) is not tuple or any(
            not isinstance(item, OcrReplicaConflict) for item in self.replica_conflicts
        ):
            raise ValueError("OCR replica conflicts must be immutable")
        if type(self.overlaps) is not tuple or any(not isinstance(item, OverlapConsensus) for item in self.overlaps):
            raise ValueError("OCR overlap consensus must be immutable")
        fusion_by_id = {item.segment_id: item for item in self.segments}
        for overlap in self.overlaps:
            if any(segment_id not in fusion_by_id for segment_id in overlap.intersection_segment_ids):
                raise ValueError("overlap consensus references an unknown segment")
            for segment_id in (
                overlap.cross_transform_confirmed_intersection_segment_ids
                + overlap.near_confirmed_intersection_segment_ids
            ):
                if fusion_by_id[segment_id].unresolved:
                    raise ValueError("resolved overlap consensus references an unresolved segment")
            for segment_id in overlap.deferred_intersection_segment_ids:
                if not fusion_by_id[segment_id].unresolved:
                    raise ValueError("deferred overlap consensus requires an unresolved segment")
        resolved_group_segments = {
            segment_id for item in self.segment_groups if not item.unresolved for segment_id in item.segment_ids
        }
        canonical_membership_slots = "overlap-contract=canonical-membership-slots" in self.diagnostics
        expected = (
            OcrFusionStatus.COMPLETE
            if all(not item.unresolved or item.segment_id in resolved_group_segments for item in self.segments)
            and not self.unassigned_word_observations
            and not self.replica_conflicts
            and (
                canonical_membership_slots
                or all(
                    not item.conflicting_intersection_segment_ids
                    and not (set(item.missing_intersection_segment_ids) - resolved_group_segments)
                    and not (set(item.deferred_intersection_segment_ids) - resolved_group_segments)
                    for item in self.overlaps
                )
            )
            else OcrFusionStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("fusion status disagrees with unresolved evidence")


def compact_ocr_text(value: str) -> str:
    """Remove only Unicode whitespace; keep case, script and codepoints exact."""

    if type(value) is not str:
        raise TypeError("OCR text must be a string")
    return "".join(character for character in value if not character.isspace())


def _reading_order_words(
    words: tuple[tuple[int, OcrWord, Box], ...],
    *,
    budget: _RoutingBudget,
) -> tuple[tuple[int, OcrWord, Box], ...]:
    """Return OCR words in stable line-major reading order.

    OCR engines do not give every word on one visual line exactly the same
    ``top`` coordinate.  Sorting by ``(top, left)`` therefore interleaves a
    line whenever glyph ascenders, punctuation, or list markers have slightly
    different boxes.  Cluster boxes that overlap vertically first, then sort
    each resulting line from left to right.

    The overlap test intentionally uses the smaller box height.  It accepts a
    short punctuation/list-marker box inside a text line without allowing the
    accumulated line bounds to bridge two separate rows.
    """

    lines: list[list[tuple[int, OcrWord, Box]]] = []
    for item in sorted(
        words,
        key=lambda value: (
            value[2].top + value[2].bottom,
            value[2].left,
            value[2].right,
            value[0],
        ),
    ):
        box = item[2]
        candidates: list[tuple[float, float, int]] = []
        for line_index, line in enumerate(lines):
            # Finding the representative scans the complete current line.  The
            # extra unit accounts for evaluating that representative against
            # the incoming word.  This makes both the one-long-line and the
            # many-singleton-lines O(words²) cases fail closed.
            budget.consume(len(line) + 1)
            representative = min(
                (member[2] for member in line),
                key=lambda value: (
                    abs((value.top + value.bottom) - (box.top + box.bottom)),
                    value.top,
                    value.left,
                ),
            )
            overlap = max(
                0,
                min(representative.bottom, box.bottom) - max(representative.top, box.top),
            )
            smaller_height = min(representative.height, box.height)
            overlap_fraction = overlap / smaller_height
            if overlap_fraction >= 0.25:
                center_distance = abs((representative.top + representative.bottom) - (box.top + box.bottom))
                candidates.append((overlap_fraction, -float(center_distance), line_index))
        if candidates:
            lines[max(candidates)[2]].append(item)
        else:
            lines.append([item])

    ordered_lines = sorted(
        lines,
        key=lambda line: (
            sum(item[2].top + item[2].bottom for item in line) / len(line),
            min(item[2].top for item in line),
            min(item[2].left for item in line),
        ),
    )
    return tuple(
        item
        for line in ordered_lines
        for item in sorted(
            line,
            key=lambda value: (
                value[2].left,
                value[2].top,
                value[2].right,
                value[2].bottom,
                value[0],
            ),
        )
    )


def align_exact_text(
    left: str,
    right: str,
    *,
    max_cells: int = 4_000_000,
) -> ExactTextAlignment:
    """Align exact non-whitespace codepoints and retain positional edits."""

    if type(max_cells) is not int or max_cells < 1:
        raise ValueError("max_cells must be a positive integer")
    compact_left = compact_ocr_text(left)
    compact_right = compact_ocr_text(right)
    cells = (len(compact_left) + 1) * (len(compact_right) + 1)
    if cells > max_cells:
        raise OcrFusionLimitError(f"exact alignment needs {cells} cells, above limit {max_cells}")
    distances = [[0] * (len(compact_right) + 1) for _ in range(len(compact_left) + 1)]
    for left_index in range(len(compact_left) + 1):
        distances[left_index][0] = left_index
    for right_index in range(len(compact_right) + 1):
        distances[0][right_index] = right_index
    for left_index in range(1, len(compact_left) + 1):
        for right_index in range(1, len(compact_right) + 1):
            distances[left_index][right_index] = min(
                distances[left_index - 1][right_index] + 1,
                distances[left_index][right_index - 1] + 1,
                distances[left_index - 1][right_index - 1]
                + int(compact_left[left_index - 1] != compact_right[right_index - 1]),
            )
    operations: list[EditOperation] = []
    left_index = len(compact_left)
    right_index = len(compact_right)
    while left_index or right_index:
        if (
            left_index
            and right_index
            and compact_left[left_index - 1] == compact_right[right_index - 1]
            and distances[left_index][right_index] == distances[left_index - 1][right_index - 1]
        ):
            operations.append(
                EditOperation(
                    EditKind.MATCH,
                    left_index - 1,
                    right_index - 1,
                    compact_left[left_index - 1],
                    compact_right[right_index - 1],
                )
            )
            left_index -= 1
            right_index -= 1
        elif (
            left_index
            and right_index
            and distances[left_index][right_index] == distances[left_index - 1][right_index - 1] + 1
        ):
            operations.append(
                EditOperation(
                    EditKind.SUBSTITUTE,
                    left_index - 1,
                    right_index - 1,
                    compact_left[left_index - 1],
                    compact_right[right_index - 1],
                )
            )
            left_index -= 1
            right_index -= 1
        elif left_index and distances[left_index][right_index] == distances[left_index - 1][right_index] + 1:
            operations.append(
                EditOperation(
                    EditKind.DELETE,
                    left_index - 1,
                    None,
                    compact_left[left_index - 1],
                    None,
                )
            )
            left_index -= 1
        else:
            operations.append(
                EditOperation(
                    EditKind.INSERT,
                    None,
                    right_index - 1,
                    None,
                    compact_right[right_index - 1],
                )
            )
            right_index -= 1
    operations.reverse()
    distance = distances[len(compact_left)][len(compact_right)]
    denominator = max(len(compact_left), len(compact_right))
    return ExactTextAlignment(
        compact_left,
        compact_right,
        tuple(operations),
        distance,
        1.0 if denominator == 0 else 1.0 - distance / denominator,
    )


class _AlignmentBudget:
    def __init__(self, config: OcrFusionConfig) -> None:
        self.config = config
        self.cells = 0
        self.logical_comparisons = 0
        self.unique_alignments = 0
        self.cache: dict[tuple[str, str], ExactTextAlignment] = {}

    def align(self, left: str, right: str) -> ExactTextAlignment:
        if self.logical_comparisons + 1 > self.config.max_pairwise_alignments:
            raise OcrFusionLimitError("exact alignments exceed the logical comparison limit")
        self.logical_comparisons += 1
        key = (compact_ocr_text(left), compact_ocr_text(right))
        existing = self.cache.get(key)
        if existing is not None:
            return existing
        cells = (len(key[0]) + 1) * (len(key[1]) + 1)
        if cells > self.config.max_alignment_cells:
            raise OcrFusionLimitError("one exact alignment exceeds its cell limit")
        if self.cells + cells > self.config.max_total_alignment_cells:
            raise OcrFusionLimitError("exact alignments exceed the aggregate cell limit")
        value = align_exact_text(key[0], key[1], max_cells=self.config.max_alignment_cells)
        self.cells += cells
        self.unique_alignments += 1
        self.cache[key] = value
        return value


class _RoutingBudget:
    def __init__(self, maximum: int, *, operation: str) -> None:
        self.maximum = maximum
        self.operation = operation
        self.comparisons = 0

    def consume(self, count: int) -> None:
        if self.comparisons + count > self.maximum:
            raise OcrFusionLimitError(f"{self.operation} exceeds its comparison limit")
        self.comparisons += count


class OcrEvidenceFusion:
    """Attribute OCR words by the configured A/B mode and fuse observations."""

    def __init__(self, config: OcrFusionConfig | None = None) -> None:
        if config is not None and not isinstance(config, OcrFusionConfig):
            raise TypeError("config must be an OcrFusionConfig")
        self.config = config or OcrFusionConfig()

    def fuse(
        self,
        *,
        plan: BlockPlan,
        segments: tuple[Segment, ...],
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
    ) -> OcrFusionResult:
        segment_by_id = self._validate_inputs(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
        )
        membership_mode = self.config.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
        routing_budget = _RoutingBudget(
            (
                self.config.max_membership_geometry_comparisons
                if membership_mode
                else self.config.max_routing_comparisons
            ),
            operation=("membership geometry correlation" if membership_mode else "word-to-segment routing"),
        )
        membership_sweep_budget = _RoutingBudget(
            self.config.max_membership_sweep_checks,
            operation="membership word sweep",
        )
        reading_order_budget = _RoutingBudget(
            self.config.max_reading_order_checks,
            operation="OCR reading-order correlation",
        )
        (
            observations,
            group_observations,
            block_text_observations,
            unassigned_word_observations,
            replica_conflicts,
        ) = self._route(
            plan=plan,
            segment_by_id=segment_by_id,
            queue=queue,
            routing_budget=routing_budget,
            membership_sweep_budget=membership_sweep_budget,
            reading_order_budget=reading_order_budget,
        )
        budget = _AlignmentBudget(self.config)
        by_segment = {segment_id: [] for segment_id in plan.source_segment_ids}
        for observation in observations:
            by_segment[observation.segment_id].append(observation)
        fusions = tuple(
            self._fuse_segment(
                segment_id,
                tuple(by_segment[segment_id]),
                budget=budget,
            )
            for segment_id in plan.source_segment_ids
        )
        group_observations_by_unit: dict[str, list[SegmentGroupObservation]] = {}
        for observation in group_observations:
            group_observations_by_unit.setdefault(observation.unit_id, []).append(observation)
        group_fusions = tuple(
            self._fuse_group(
                unit,
                tuple(group_observations_by_unit.get(unit.unit_id, ())),
                budget=budget,
            )
            for unit in plan.membership_units
            if unit.kind is MembershipUnitKind.SUBBLOCK
        )
        overlaps = self._overlaps(
            plan=plan,
            observations=observations,
            fusions=fusions,
            budget=budget,
        )
        resolved_group_segments = {
            segment_id for item in group_fusions if not item.unresolved for segment_id in item.segment_ids
        }
        unresolved = sum(item.unresolved and item.segment_id not in resolved_group_segments for item in fusions)
        conflicts = sum(len(item.conflicting_intersection_segment_ids) for item in overlaps)
        missing = sum(len(item.missing_intersection_segment_ids) for item in overlaps)
        uncovered_missing = sum(
            len(set(item.missing_intersection_segment_ids) - resolved_group_segments) for item in overlaps
        )
        exact_confirmed = sum(len(item.confirmed_intersection_segment_ids) for item in overlaps)
        cross_transform_confirmed = sum(
            len(item.cross_transform_confirmed_intersection_segment_ids) for item in overlaps
        )
        near_confirmed = sum(len(item.near_confirmed_intersection_segment_ids) for item in overlaps)
        deferred = sum(len(item.deferred_intersection_segment_ids) for item in overlaps)
        uncovered_deferred = sum(
            len(set(item.deferred_intersection_segment_ids) - resolved_group_segments) for item in overlaps
        )
        canonical_membership_slots = self._canonical_membership_slots(
            plan,
            queue,
        )
        status = (
            OcrFusionStatus.COMPLETE
            if unresolved == 0
            and (canonical_membership_slots or (conflicts == 0 and uncovered_missing == 0 and uncovered_deferred == 0))
            and not unassigned_word_observations
            and not replica_conflicts
            else OcrFusionStatus.UNRESOLVED
        )
        return OcrFusionResult(
            source_segment_ids=plan.source_segment_ids,
            observations=observations,
            block_text_observations=block_text_observations,
            unassigned_word_observations=unassigned_word_observations,
            replica_conflicts=replica_conflicts,
            segments=fusions,
            overlaps=overlaps,
            status=status,
            diagnostics=(
                "reference-evidence=forbidden",
                "text-comparison=exact-codepoints-without-whitespace",
                f"routing-mode={self.config.routing_mode.value}",
                ("or-xor=observed-block-membership-signatures" if membership_mode else "or-xor=geometry-only"),
                (
                    "membership-completeness=explicit-complete-job-matrix"
                    if membership_mode and self.config.membership_assume_complete_observations
                    else (
                        "membership-completeness=omission-safe"
                        if membership_mode
                        else "membership-completeness=not-applicable"
                    )
                ),
                (
                    "membership-per-word-omission-risk=" "accepted-by-explicit-profile"
                    if membership_mode and self.config.membership_assume_complete_observations
                    else (
                        "membership-per-word-omission-risk=not-accepted"
                        if membership_mode
                        else "membership-per-word-omission-risk=not-applicable"
                    )
                ),
                (
                    "overlap-contract=canonical-membership-slots"
                    if canonical_membership_slots
                    else "overlap-contract=cartesian-source-geometry"
                ),
                f"alignment-cells={budget.cells}",
                f"alignment-logical-comparisons={budget.logical_comparisons}",
                f"alignment-unique-pairs={budget.unique_alignments}",
                f"routing-comparisons={routing_budget.comparisons}",
                "membership-sweep-checks=" f"{membership_sweep_budget.comparisons}",
                "reading-order-checks=" f"{reading_order_budget.comparisons}",
                f"unassigned-words={len(unassigned_word_observations)}",
                f"segment-group-observations={len(group_observations)}",
                f"segment-groups={len(group_fusions)}",
                f"replica-conflicts={len(replica_conflicts)}",
                f"overlap-exact-confirmed={exact_confirmed}",
                f"overlap-cross-transform-confirmed={cross_transform_confirmed}",
                f"overlap-near-confirmed={near_confirmed}",
                f"overlap-deferred={deferred}",
                f"overlap-deferred-uncovered={uncovered_deferred}",
                f"overlap-conflicts={conflicts}",
                f"overlap-missing={missing}",
                f"overlap-missing-uncovered={uncovered_missing}",
            ),
            group_observations=group_observations,
            segment_groups=group_fusions,
        )

    @staticmethod
    def _canonical_membership_slots(
        plan: BlockPlan,
        queue: OcrQueueResult,
    ) -> bool:
        return any("geometry=canonical-membership-slots-v1" in diagnostic for diagnostic in queue.diagnostics) and all(
            block.matrix_window_kind in {"polar-local-full", "polar-local-signature"} for block in plan.blocks
        )

    @classmethod
    def _canonical_membership_slot_size(
        cls,
        plan: BlockPlan,
        queue: OcrQueueResult,
    ) -> tuple[int, int] | None:
        if not cls._canonical_membership_slots(plan, queue):
            return None
        sizes: set[tuple[int, int]] = set()
        for diagnostic in queue.diagnostics:
            fields = dict(field.split("=", 1) for field in diagnostic.split(";") if "=" in field)
            if fields.get("geometry") != "canonical-membership-slots-v1":
                continue
            width_text = fields.get("width")
            height_text = fields.get("height")
            if width_text is None and height_text is None:
                continue
            try:
                width = int(width_text or "")
                height = int(height_text or "")
            except ValueError as error:
                raise OcrFusionInvariantError("canonical membership slot bound is malformed") from error
            if width <= 0 or height <= 0:
                raise OcrFusionInvariantError("canonical membership slot bound must be positive")
            sizes.add((width, height))
        if len(sizes) > 1:
            raise OcrFusionInvariantError("canonical membership slot bounds disagree")
        if not sizes:
            return None
        size = next(iter(sizes))
        if len(plan.membership_units) > size[0] * size[1]:
            raise OcrFusionInvariantError("canonical membership slots exceed their declared bound")
        return size

    @staticmethod
    def _bound_png_size(crop: BlockCropPair) -> tuple[int, int]:
        """Read the immutable hash-bound PNG canvas without decoding pixels."""

        payload = crop.raw.png_bytes
        if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
            raise OcrFusionInvariantError("canonical membership crop has invalid PNG geometry")
        width = int.from_bytes(payload[16:20], "big")
        height = int.from_bytes(payload[20:24], "big")
        if width < 1 or height < 1:
            raise OcrFusionInvariantError("canonical membership crop has invalid PNG geometry")
        return width, height

    def _validate_inputs(
        self,
        *,
        plan: BlockPlan,
        segments: tuple[Segment, ...],
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
    ) -> dict[str, Segment]:
        if not isinstance(plan, BlockPlan):
            raise OcrFusionInvariantError("plan must be a BlockPlan")
        if type(segments) is not tuple or any(not isinstance(segment, Segment) for segment in segments):
            raise OcrFusionInvariantError("segments must be an immutable Segment tuple")
        if not isinstance(queue, OcrQueueResult):
            raise OcrFusionInvariantError("queue must be an OcrQueueResult")
        if len(queue.jobs) > self.config.max_jobs:
            raise OcrFusionLimitError("OCR job count exceeds configured fusion limit")
        if type(crops) is not tuple or any(not isinstance(crop, BlockCropPair) for crop in crops):
            raise OcrFusionInvariantError("crops must be an immutable BlockCropPair tuple")
        if len(segments) > self.config.max_segments:
            raise OcrFusionLimitError("segment count exceeds configured limit")
        segment_by_id = {segment.segment_id: segment for segment in segments}
        if len(segment_by_id) != len(segments) or set(segment_by_id) != set(plan.source_segment_ids):
            raise OcrFusionInvariantError("segments and block plan disagree")
        if len(crops) != len(plan.blocks):
            raise OcrFusionInvariantError("block plan and crop count disagree")
        for block, crop in zip(plan.blocks, crops):
            if crop.block_id != block.block_id or crop.bbox != block.bbox or crop.segment_ids != block.segment_ids:
                raise OcrFusionInvariantError("block plan and crop provenance disagree")
        for index, job in enumerate(queue.jobs):
            if job.job_id != f"ocr-job-{index:08d}":
                raise OcrFusionInvariantError("OCR queue jobs are not in canonical order")
        if self.config.require_exact_job_matrix:
            self._validate_job_matrix(plan=plan, crops=crops, queue=queue)
        else:
            self._validate_sparse_jobs(plan=plan, crops=crops, queue=queue)
        if (
            self.config.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP
            and self.config.membership_assume_complete_observations
        ):
            self._validate_complete_membership_job_matrix(queue)
        return segment_by_id

    def _validate_sparse_jobs(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
    ) -> None:
        block_by_id = {block.block_id: block for block in plan.blocks}
        crop_by_id = {crop.block_id: crop for crop in crops}
        canonical_membership = self._canonical_membership_slots(plan, queue)
        canonical_slot_size = self._canonical_membership_slot_size(
            plan,
            queue,
        )
        seen: set[tuple[str, OcrTransform, str]] = set()
        lane_contract: dict[str, tuple[object, str]] = {}
        for job in queue.jobs:
            if job.block_id not in block_by_id:
                raise OcrFusionInvariantError("sparse OCR job is outside the block plan")
            key = (job.block_id, job.transform, job.lane_id)
            if key in seen:
                raise OcrFusionInvariantError("sparse OCR jobs contain a duplicate block-transform-lane")
            seen.add(key)
            contract = (job.resource, job.capability_id)
            previous = lane_contract.setdefault(job.lane_id, contract)
            if previous != contract:
                raise OcrFusionInvariantError("OCR lane capability changed within one sparse queue")
            if len(lane_contract) > self.config.max_lanes:
                raise OcrFusionLimitError("OCR lane count exceeds configured fusion limit")
            crop = crop_by_id[job.block_id]
            expected_input = (
                crop.raw.png_bytes
                if job.transform
                in (
                    OcrTransform.RAW,
                    OcrTransform.CONTEXTUAL_COMPOSITE,
                )
                else crop.gamma.png_bytes if crop.gamma is not None else None
            )
            context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
            if (
                expected_input is not None and job.input_sha256 != hashlib.sha256(expected_input).hexdigest()
            ) or job.context_sha256 != context_sha256:
                raise OcrFusionInvariantError("sparse OCR job is not bound to its block crop")
            if job.status is OcrJobStatus.COMPLETE:
                if job.output is None:
                    raise OcrFusionInvariantError("complete sparse OCR job lost its output")
                if job.output.geometry is OcrOutputGeometry.WORD_BOXES:
                    if canonical_membership:
                        bound_width, bound_height = self._bound_png_size(crop)
                        if canonical_slot_size is not None and (
                            canonical_slot_size[0] > bound_width or canonical_slot_size[1] > bound_height
                        ):
                            raise OcrFusionInvariantError(
                                "canonical membership slot canvas exceeds " "bound crop raster"
                            )
                        canvas_size = canonical_slot_size or (
                            bound_width,
                            bound_height,
                        )
                    else:
                        canvas_size = (crop.bbox.width, crop.bbox.height)
                    canvas = Box(
                        0,
                        0,
                        *canvas_size,
                    )
                    if any(word.bbox.intersection(canvas) != word.bbox for word in job.output.words):
                        raise OcrFusionInvariantError("sparse OCR word bbox is outside its bound block crop")

    @staticmethod
    def _validate_complete_membership_job_matrix(
        queue: OcrQueueResult,
    ) -> None:
        if any(
            job.status is not OcrJobStatus.COMPLETE
            or job.output is None
            or job.output.geometry is not OcrOutputGeometry.WORD_BOXES
            for job in queue.jobs
        ):
            raise OcrFusionInvariantError(
                "complete-observation membership routing requires every "
                "RAW/GAMMA block job to complete with word boxes"
            )

    def _validate_job_matrix(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
    ) -> None:
        if len(queue.jobs) > self.config.max_jobs:
            raise OcrFusionLimitError("OCR job count exceeds configured fusion limit")
        if not plan.blocks:
            if queue.jobs:
                raise OcrFusionInvariantError("empty plan cannot contain OCR jobs")
            return
        first_block_id = plan.blocks[0].block_id
        lane_values: list[str] = []
        lane_set: set[str] = set()
        for job in queue.jobs:
            if job.block_id != first_block_id or job.transform is not OcrTransform.RAW:
                continue
            if job.lane_id in lane_set:
                raise OcrFusionInvariantError("OCR job matrix has invalid lane scope")
            if len(lane_values) + 1 > self.config.max_lanes:
                raise OcrFusionLimitError("OCR lane count exceeds configured fusion limit")
            lane_values.append(job.lane_id)
            lane_set.add(job.lane_id)
        lane_ids = tuple(lane_values)
        if not lane_ids:
            raise OcrFusionInvariantError("OCR job matrix has invalid lane scope")
        jobs_per_block = 2 * len(lane_ids)
        expected_job_count = len(plan.blocks) * jobs_per_block
        if expected_job_count > self.config.max_jobs:
            raise OcrFusionLimitError("OCR block-transform-lane matrix exceeds configured fusion limit")
        if len(queue.jobs) != expected_job_count:
            raise OcrFusionInvariantError("OCR jobs are not the exact block-transform-lane matrix")
        for index, job in enumerate(queue.jobs):
            block_index, within_block = divmod(index, jobs_per_block)
            transform_index, lane_index = divmod(
                within_block,
                len(lane_ids),
            )
            expected_transform = OcrTransform.RAW if transform_index == 0 else OcrTransform.GAMMA
            if (
                job.block_id != plan.blocks[block_index].block_id
                or job.transform is not expected_transform
                or job.lane_id != lane_ids[lane_index]
            ):
                raise OcrFusionInvariantError("OCR jobs are not the exact block-transform-lane matrix")
        lane_contract: dict[str, tuple[object, str]] = {}
        crop_by_id = {crop.block_id: crop for crop in crops}
        for job in queue.jobs:
            contract = (job.resource, job.capability_id)
            previous = lane_contract.setdefault(job.lane_id, contract)
            if previous != contract:
                raise OcrFusionInvariantError("OCR lane capability changed within one matrix")
            crop = crop_by_id[job.block_id]
            expected_input = (
                crop.raw.png_bytes
                if job.transform
                in (
                    OcrTransform.RAW,
                    OcrTransform.CONTEXTUAL_COMPOSITE,
                )
                else crop.gamma.png_bytes
            )
            input_sha256 = hashlib.sha256(expected_input).hexdigest()
            context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
            if job.input_sha256 != input_sha256 or job.context_sha256 != context_sha256:
                raise OcrFusionInvariantError("OCR job is not bound to its block crop")
            if job.status is OcrJobStatus.COMPLETE:
                if job.output is None:
                    raise OcrFusionInvariantError("complete OCR job lost its output")
                if job.output.geometry is OcrOutputGeometry.WORD_BOXES:
                    canvas = Box(0, 0, crop.bbox.width, crop.bbox.height)
                    if any(word.bbox.intersection(canvas) != word.bbox for word in job.output.words):
                        raise OcrFusionInvariantError("OCR word bbox is outside its bound block crop")

    def _route(
        self,
        *,
        plan: BlockPlan,
        segment_by_id: dict[str, Segment],
        queue: OcrQueueResult,
        routing_budget: _RoutingBudget,
        membership_sweep_budget: _RoutingBudget,
        reading_order_budget: _RoutingBudget,
    ) -> tuple[
        tuple[SegmentObservation, ...],
        tuple[SegmentGroupObservation, ...],
        tuple[BlockTextObservation, ...],
        tuple[UnassignedWordObservation, ...],
        tuple[OcrReplicaConflict, ...],
    ]:
        if self.config.routing_mode is OcrRoutingMode.BLOCK_MEMBERSHIP:
            return self._route_by_membership(
                plan=plan,
                queue=queue,
                routing_budget=routing_budget,
                membership_sweep_budget=membership_sweep_budget,
                reading_order_budget=reading_order_budget,
            )
        block_by_id = {block.block_id: block for block in plan.blocks}
        conflict_keys, replica_conflicts = self._replica_conflicts(queue)
        observations: list[SegmentObservation] = []
        block_text_observations: list[BlockTextObservation] = []
        unassigned_word_observations: list[UnassignedWordObservation] = []
        for job in queue.jobs:
            if job.status is not OcrJobStatus.COMPLETE:
                continue
            if job.output is None:
                raise OcrFusionInvariantError("complete OCR job lost its output")
            block = block_by_id[job.block_id]
            source_key = (
                job.context_sha256,
                job.capability_id,
                job.transform,
            )
            source_replica_conflict = source_key in conflict_keys
            if job.output.geometry is OcrOutputGeometry.TEXT_ONLY:
                block_text_observations.append(
                    BlockTextObservation(
                        job_id=job.job_id,
                        block_id=job.block_id,
                        transform=job.transform,
                        lane_id=job.lane_id,
                        capability_id=job.capability_id,
                        text=job.output.text,
                        attribution_status=OcrAttributionStatus.UNATTRIBUTABLE,
                        attribution_reason="no-observed-bbox",
                        input_sha256=job.input_sha256,
                        context_sha256=job.context_sha256,
                        source_replica_conflict=source_replica_conflict,
                    )
                )
                continue
            if not job.output.words or not job.output.text.strip():
                raise OcrFusionInvariantError("complete bbox OCR job contains no attributable words")
            routed: dict[str, list[tuple[int, OcrWord, Box]]] = {segment_id: [] for segment_id in block.segment_ids}
            members = tuple(segment_by_id[item] for item in block.segment_ids)
            for word_index, word in enumerate(job.output.words):
                page_bbox = Box(
                    word.bbox.left + block.bbox.left,
                    word.bbox.top + block.bbox.top,
                    word.bbox.right + block.bbox.left,
                    word.bbox.bottom + block.bbox.top,
                )
                target, unassigned_reason = self._route_word(
                    page_bbox,
                    members,
                    routing_budget,
                )
                if target is not None:
                    routed[target.segment_id].append((word_index, word, page_bbox))
                else:
                    unassigned_word_observations.append(
                        UnassignedWordObservation(
                            job_id=job.job_id,
                            block_id=job.block_id,
                            transform=job.transform,
                            lane_id=job.lane_id,
                            capability_id=job.capability_id,
                            word_index=word_index,
                            text=word.text,
                            confidence=word.confidence,
                            page_bbox=page_bbox,
                            input_sha256=job.input_sha256,
                            context_sha256=job.context_sha256,
                            reason=unassigned_reason or "no-segment-intersection",
                        )
                    )
            for segment_id in block.segment_ids:
                words = _reading_order_words(
                    tuple(routed[segment_id]),
                    budget=reading_order_budget,
                )
                text = " ".join(word.text for _, word, _ in words)
                if len(text) > self.config.max_text_chars:
                    raise OcrFusionLimitError("routed segment text exceeds configured limit")
                confidence = sum(float(word.confidence) for _, word, _ in words) / len(words) if words else 0.0
                observations.append(
                    SegmentObservation(
                        observation_id=f"observation-{len(observations):08d}",
                        job_id=job.job_id,
                        segment_id=segment_id,
                        block_id=block.block_id,
                        transform=job.transform,
                        lane_id=job.lane_id,
                        capability_id=job.capability_id,
                        text=text,
                        confidence=confidence,
                        page_bboxes=tuple(page_bbox for _, _, page_bbox in words),
                        input_sha256=job.input_sha256,
                        context_sha256=job.context_sha256,
                        source_replica_conflict=source_replica_conflict,
                    )
                )
                if len(observations) > self.config.max_observations:
                    raise OcrFusionLimitError("routed observations exceed configured limit")
        return (
            tuple(observations),
            (),
            tuple(block_text_observations),
            tuple(unassigned_word_observations),
            replica_conflicts,
        )

    def _route_by_membership(
        self,
        *,
        plan: BlockPlan,
        queue: OcrQueueResult,
        routing_budget: _RoutingBudget,
        membership_sweep_budget: _RoutingBudget,
        reading_order_budget: _RoutingBudget,
    ) -> tuple[
        tuple[SegmentObservation, ...],
        tuple[SegmentGroupObservation, ...],
        tuple[BlockTextObservation, ...],
        tuple[UnassignedWordObservation, ...],
        tuple[OcrReplicaConflict, ...],
    ]:
        """Decode words from exact observed block-membership signatures.

        Stage 1 segment boxes are deliberately absent from this method.  OCR
        boxes are projected only by their block crop offset and correlated
        with other *observed* OCR boxes.  Raw/gamma and lane replicas from one
        block consequently contribute one set-membership bit, never multiple
        votes.
        """

        block_by_id = {block.block_id: block for block in plan.blocks}
        conflict_keys, replica_conflicts = self._replica_conflicts(queue)
        canonical_membership_slots = self._canonical_membership_slots(
            plan,
            queue,
        )
        projected: list[_ProjectedWordObservation] = []
        block_text_observations: list[BlockTextObservation] = []

        for job in queue.jobs:
            if job.status is not OcrJobStatus.COMPLETE:
                continue
            if job.output is None:
                raise OcrFusionInvariantError("complete OCR job lost its output")
            block = block_by_id[job.block_id]
            source_key = (
                job.context_sha256,
                job.capability_id,
                job.transform,
            )
            source_replica_conflict = source_key in conflict_keys
            if job.output.geometry is OcrOutputGeometry.TEXT_ONLY:
                block_text_observations.append(
                    BlockTextObservation(
                        job_id=job.job_id,
                        block_id=job.block_id,
                        transform=job.transform,
                        lane_id=job.lane_id,
                        capability_id=job.capability_id,
                        text=job.output.text,
                        attribution_status=OcrAttributionStatus.UNATTRIBUTABLE,
                        attribution_reason="no-observed-bbox",
                        input_sha256=job.input_sha256,
                        context_sha256=job.context_sha256,
                        source_replica_conflict=source_replica_conflict,
                    )
                )
                continue
            if not job.output.words or not job.output.text.strip():
                raise OcrFusionInvariantError("complete bbox OCR job contains no attributable words")
            for word_index, word in enumerate(job.output.words):
                projected.append(
                    _ProjectedWordObservation(
                        ordinal=len(projected),
                        job=job,
                        word_index=word_index,
                        word=word,
                        page_bbox=(
                            word.bbox
                            if canonical_membership_slots
                            else Box(
                                word.bbox.left + block.bbox.left,
                                word.bbox.top + block.bbox.top,
                                word.bbox.right + block.bbox.left,
                                word.bbox.bottom + block.bbox.top,
                            )
                        ),
                        source_replica_conflict=source_replica_conflict,
                    )
                )
                if len(projected) > self.config.max_observations:
                    raise OcrFusionLimitError("projected membership words exceed configured observation limit")

        if not plan.membership_units and plan.source_segment_ids:
            raise OcrFusionInvariantError("block-membership routing requires canonical membership units")
        membership_assignments = 0
        for block in plan.blocks:
            membership_assignments += len(block.segment_ids)
            if membership_assignments > self.config.max_membership_block_assignments:
                raise OcrFusionLimitError("block membership assignments exceed configured limit")
        expected_by_signature: dict[frozenset[str], MembershipUnit] = {
            frozenset(unit.block_ids): unit for unit in plan.membership_units
        }
        if len(expected_by_signature) != len(plan.membership_units):
            raise OcrFusionInvariantError("membership unit signatures are not unique")
        expected_transforms: dict[tuple[str, str], set[OcrTransform]] = {}
        for job in queue.jobs:
            expected_transforms.setdefault((job.block_id, job.capability_id), set()).add(job.transform)

        canonical_membership_slots = self._canonical_membership_slots(
            plan,
            queue,
        )
        decisions: dict[int, tuple[MembershipUnit | None, str | None]] = {}
        signature_decisions: dict[frozenset[str], tuple[MembershipUnit | None, str | None]] = {}
        signature_comparisons = 0
        for members, geometry_ambiguous in self._membership_geometry_clusters(
            tuple(projected),
            budget=routing_budget,
            sweep_budget=membership_sweep_budget,
        ):
            signature = frozenset(projected[index].job.block_id for index in members)
            candidate = expected_by_signature.get(signature)
            if geometry_ambiguous:
                decision = (None, "membership-geometry-ambiguous")
                for index in members:
                    decisions[index] = decision
                continue
            observed_transforms: dict[tuple[str, str], set[OcrTransform]] = {}
            for index in members:
                job = projected[index].job
                observed_transforms.setdefault((job.block_id, job.capability_id), set()).add(job.transform)
            observed_capabilities = {capability_id for _block_id, capability_id in observed_transforms}
            complete_observation_lattice = False
            if candidate is not None and self.config.membership_assume_complete_observations:
                for capability_id in sorted(observed_capabilities):
                    capability_complete = True
                    for block_id in candidate.block_ids:
                        signature_comparisons += 1
                        if signature_comparisons > self.config.max_membership_signature_comparisons:
                            raise OcrFusionLimitError("membership signature comparisons exceed " "configured limit")
                        expected = expected_transforms.get((block_id, capability_id), set())
                        if not expected or observed_transforms.get((block_id, capability_id), set()) != expected:
                            capability_complete = False
                            break
                    if capability_complete:
                        complete_observation_lattice = True
                        break
            spatial_omission_ambiguous = False
            if (
                candidate is not None
                and self.config.membership_assume_complete_observations
                and complete_observation_lattice
                and not canonical_membership_slots
            ):
                for expected_signature in expected_by_signature:
                    signature_comparisons += 1
                    if signature_comparisons > self.config.max_membership_signature_comparisons:
                        raise OcrFusionLimitError("membership signature comparisons exceed " "configured limit")
                    if not signature < expected_signature:
                        continue
                    additional_blocks = tuple(
                        block_id
                        for block_id in block_by_id
                        if block_id in expected_signature and block_id not in signature
                    )
                    all_missing_crops_contain_word = True
                    for block_id in additional_blocks:
                        crop_contains_word = False
                        for index in members:
                            routing_budget.consume(1)
                            if (
                                block_by_id[block_id].bbox.intersection(projected[index].page_bbox)
                                == projected[index].page_bbox
                            ):
                                crop_contains_word = True
                                break
                        if not crop_contains_word:
                            all_missing_crops_contain_word = False
                            break
                    if all_missing_crops_contain_word:
                        spatial_omission_ambiguous = True
                        break
            if (
                candidate is not None
                and self.config.membership_assume_complete_observations
                and not complete_observation_lattice
            ):
                # A completed OCR job is not proof that every token was
                # observed.  Without this per-token lattice check, a token
                # missing from one transform/capability can impersonate a
                # shorter block-membership code (table rules such as ``|``
                # are a common example).
                decision = (
                    None,
                    "membership-observation-lattice-incomplete",
                )
            elif spatial_omission_ambiguous:
                # A full RAW/GAMMA lattice for a short code is still
                # ambiguous when the observed token lies inside every missing
                # crop of a longer valid code.  This uses only OCR geometry
                # and block bounds, never a Stage 1 segment box.
                decision = (
                    None,
                    "membership-signature-omission-ambiguous",
                )
            else:
                cached = signature_decisions.get(signature)
                if cached is not None:
                    decision = cached
                elif candidate is None:
                    decision = (None, "membership-signature-unmatched")
                else:
                    omission_ambiguous = False
                    if not self.config.membership_assume_complete_observations and not canonical_membership_slots:
                        for expected_signature in expected_by_signature:
                            signature_comparisons += 1
                            if signature_comparisons > self.config.max_membership_signature_comparisons:
                                raise OcrFusionLimitError("membership signature comparisons exceed " "configured limit")
                            if signature < expected_signature:
                                omission_ambiguous = True
                                break
                    if omission_ambiguous:
                        # An exact shorter code is not proof of identity: it
                        # may be a longer source code with OCR contexts omitted.
                        decision = (
                            None,
                            "membership-signature-omission-ambiguous",
                        )
                    else:
                        decision = (candidate, None)
                signature_decisions[signature] = decision
            for index in members:
                decisions[index] = decision

        assigned: dict[tuple[str, str], list[tuple[int, OcrWord, Box]]] = {}
        unassigned_word_observations: list[UnassignedWordObservation] = []
        for item in projected:
            unit, reason = decisions[item.ordinal]
            job = item.job
            if unit is not None:
                assigned.setdefault((job.job_id, unit.unit_id), []).append((item.word_index, item.word, item.page_bbox))
                continue
            unassigned_word_observations.append(
                UnassignedWordObservation(
                    job_id=job.job_id,
                    block_id=job.block_id,
                    transform=job.transform,
                    lane_id=job.lane_id,
                    capability_id=job.capability_id,
                    word_index=item.word_index,
                    text=item.word.text,
                    confidence=item.word.confidence,
                    page_bbox=item.page_bbox,
                    input_sha256=job.input_sha256,
                    context_sha256=job.context_sha256,
                    reason=reason or "membership-signature-unmatched",
                )
            )

        units_by_block: dict[str, list[MembershipUnit]] = {block.block_id: [] for block in plan.blocks}
        for unit in plan.membership_units:
            for block_id in unit.block_ids:
                units_by_block[block_id].append(unit)
        observations: list[SegmentObservation] = []
        group_observations: list[SegmentGroupObservation] = []
        for job in queue.jobs:
            if (
                job.status is not OcrJobStatus.COMPLETE
                or job.output is None
                or job.output.geometry is OcrOutputGeometry.TEXT_ONLY
            ):
                continue
            block = block_by_id[job.block_id]
            source_replica_conflict = (
                job.context_sha256,
                job.capability_id,
                job.transform,
            ) in conflict_keys
            for unit in units_by_block[block.block_id]:
                words = _reading_order_words(
                    tuple(assigned.get((job.job_id, unit.unit_id), ())),
                    budget=reading_order_budget,
                )
                text = " ".join(word.text for _, word, _ in words)
                if len(text) > self.config.max_text_chars:
                    raise OcrFusionLimitError("routed segment text exceeds configured limit")
                confidence = sum(float(word.confidence) for _, word, _ in words) / len(words) if words else 0.0
                common = {
                    "job_id": job.job_id,
                    "block_id": block.block_id,
                    "transform": job.transform,
                    "lane_id": job.lane_id,
                    "capability_id": job.capability_id,
                    "text": text,
                    "confidence": confidence,
                    "page_bboxes": tuple(page_bbox for _, _, page_bbox in words),
                    "input_sha256": job.input_sha256,
                    "context_sha256": job.context_sha256,
                    "source_replica_conflict": source_replica_conflict,
                }
                if unit.kind is MembershipUnitKind.SEGMENT:
                    observations.append(
                        SegmentObservation(
                            observation_id=(f"observation-{len(observations):08d}"),
                            segment_id=unit.segment_ids[0],
                            **common,
                        )
                    )
                else:
                    group_observations.append(
                        SegmentGroupObservation(
                            observation_id=("group-observation-" f"{len(group_observations):08d}"),
                            unit_id=unit.unit_id,
                            segment_ids=unit.segment_ids,
                            **common,
                        )
                    )
                if len(observations) + len(group_observations) > self.config.max_observations:
                    raise OcrFusionLimitError("routed observations exceed configured limit")
        return (
            tuple(observations),
            tuple(group_observations),
            tuple(block_text_observations),
            tuple(unassigned_word_observations),
            replica_conflicts,
        )

    def _membership_geometry_clusters(
        self,
        projected: tuple[_ProjectedWordObservation, ...],
        *,
        budget: _RoutingBudget,
        sweep_budget: _RoutingBudget,
    ) -> tuple[tuple[tuple[int, ...], bool], ...]:
        """Return complete-link geometry clusters and ambiguity flags.

        The sweep considers only horizontally/vertically overlapping boxes.
        A final complete-link check rejects transitive bridges, and two OCR
        tokens from one job in one component reject a many-to-one merge.
        """

        if not projected:
            return ()
        parent = list(range(len(projected)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first: int, second: int) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                if first_root > second_root:
                    first_root, second_root = second_root, first_root
                parent[second_root] = first_root

        comparison_cache: dict[tuple[int, int], bool] = {}

        def compatible(first: int, second: int) -> bool:
            key = (first, second) if first < second else (second, first)
            existing = comparison_cache.get(key)
            if existing is not None:
                return existing
            budget.consume(1)
            value = self._membership_boxes_match(
                projected[key[0]].page_bbox,
                projected[key[1]].page_bbox,
            )
            comparison_cache[key] = value
            return value

        active: dict[int, None] = {}
        active_by_right: list[tuple[int, int]] = []
        order = sorted(
            range(len(projected)),
            key=lambda index: (
                projected[index].page_bbox.left,
                projected[index].page_bbox.top,
                projected[index].page_bbox.right,
                projected[index].page_bbox.bottom,
                index,
            ),
        )
        for index in order:
            box = projected[index].page_bbox
            while active_by_right and active_by_right[0][0] <= box.left:
                _right, expired = heapq.heappop(active_by_right)
                active.pop(expired, None)
            for other in active:
                sweep_budget.consume(1)
                other_box = projected[other].page_bbox
                if min(other_box.bottom, box.bottom) <= max(other_box.top, box.top):
                    continue
                if compatible(other, index):
                    union(other, index)
            active[index] = None
            heapq.heappush(active_by_right, (box.right, index))

        components: dict[int, list[int]] = {}
        for index in range(len(projected)):
            components.setdefault(find(index), []).append(index)

        result: list[tuple[tuple[int, ...], bool]] = []
        for members_list in sorted(components.values(), key=lambda values: min(values)):
            members = tuple(sorted(members_list))
            job_ids = tuple(projected[index].job.job_id for index in members)
            ambiguous = len(job_ids) != len(set(job_ids))
            if not ambiguous:
                for position, first in enumerate(members):
                    if any(not compatible(first, second) for second in members[position + 1 :]):
                        ambiguous = True
                        break
            result.append((members, ambiguous))
        return tuple(result)

    def _membership_boxes_match(self, first: Box, second: Box) -> bool:
        intersection = first.intersection_area(second)
        if intersection <= 0:
            return False
        union = first.area + second.area - intersection
        if intersection / union < self.config.minimum_membership_bbox_iou:
            return False
        first_center_x, first_center_y = first.center
        second_center_x, second_center_y = second.center
        return abs(first_center_x - second_center_x) <= self.config.maximum_membership_center_distance_fraction * min(
            first.width, second.width
        ) and abs(first_center_y - second_center_y) <= self.config.maximum_membership_center_distance_fraction * min(
            first.height, second.height
        )

    @staticmethod
    def _replica_conflicts(
        queue: OcrQueueResult,
    ) -> tuple[
        set[tuple[str, str, OcrTransform]],
        tuple[OcrReplicaConflict, ...],
    ]:
        grouped: dict[
            tuple[str, str, OcrTransform],
            list[tuple[str, str]],
        ] = {}
        for job in queue.jobs:
            if job.status is OcrJobStatus.COMPLETE:
                if job.output is None:
                    raise OcrFusionInvariantError("complete OCR job lost its output")
                evidence: dict[str, object] = {
                    "status": job.status.value,
                    "input_sha256": job.input_sha256,
                    "geometry": job.output.geometry.value,
                    "text": job.output.text,
                    "words": [
                        {
                            "text": word.text,
                            "bbox": list(word.bbox.as_tuple()),
                            "confidence": float(word.confidence),
                        }
                        for word in job.output.words
                    ],
                }
            else:
                evidence = {
                    "status": job.status.value,
                    "input_sha256": job.input_sha256,
                    "failure_code": (job.failure_code.value if job.failure_code is not None else None),
                    "error_type": job.error_type,
                }
            digest = hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            key = (
                job.context_sha256,
                job.capability_id,
                job.transform,
            )
            grouped.setdefault(key, []).append((job.job_id, digest))
        conflicts: list[OcrReplicaConflict] = []
        keys: set[tuple[str, str, OcrTransform]] = set()
        for key, values in grouped.items():
            signatures = tuple(dict.fromkeys(digest for _, digest in values))
            if len(values) > 1 and len(signatures) > 1:
                keys.add(key)
                conflicts.append(
                    OcrReplicaConflict(
                        context_sha256=key[0],
                        capability_id=key[1],
                        transform=key[2],
                        job_ids=tuple(job_id for job_id, _ in values),
                        evidence_sha256=signatures,
                    )
                )
        return keys, tuple(conflicts)

    def _route_word(
        self,
        page_bbox: Box,
        members: tuple[Segment, ...],
        budget: _RoutingBudget,
    ) -> tuple[Segment | None, str | None]:
        budget.consume(len(members))
        ranked = tuple(
            (
                segment.bbox.intersection_area(page_bbox),
                -index,
                segment,
            )
            for index, segment in enumerate(members)
        )
        positive = tuple(item for item in ranked if item[0] > 0)
        if not positive:
            return None, "no-segment-intersection"
        word_area = page_bbox.width * page_bbox.height
        significant = tuple(
            item for item in positive if item[0] / word_area >= self.config.minimum_significant_overlap_fraction
        )
        candidates = significant or positive
        if len(candidates) == 1:
            return candidates[0][2], None
        center_x, center_y = page_bbox.center
        center_owners = tuple(item for item in candidates if item[2].bbox.contains_point(center_x, center_y))
        if len(center_owners) == 1:
            return center_owners[0][2], None
        return None, "ambiguous-segment-intersection"

    def _fuse_segment(
        self,
        segment_id: str,
        observations: tuple[SegmentObservation, ...],
        *,
        budget: _AlignmentBudget,
    ) -> SegmentFusion:
        raw = self._independent_observations(
            tuple(item for item in observations if item.transform is OcrTransform.RAW and item.comparison_text)
        )
        raw_choice, raw_confidence, stability = self._observed_medoid(raw, budget)
        exact_context_majority = self._has_exact_context_majority(
            raw,
            selected=raw_choice,
        )
        script_confidence_override = self._script_confidence_override(raw)
        if script_confidence_override is not None and (
            raw_choice is None or script_confidence_override[0].observation_id != raw_choice.observation_id
        ):
            raw_choice, raw_confidence = script_confidence_override
        else:
            script_confidence_override = None
        alternative, alternative_confidence = self._stable_alternative(
            observations,
            control=raw_choice,
            control_confidence=raw_confidence,
        )
        selected = raw_choice
        selected_confidence = raw_confidence if raw_choice is not None else None
        selected_capability_raw = tuple(
            item for item in raw if raw_choice is not None and item.capability_id == raw_choice.capability_id
        )
        _, _, selected_capability_stability = self._observed_medoid(
            selected_capability_raw,
            budget,
        )
        selected_capability_contexts = {item.context_sha256 for item in selected_capability_raw}
        transform_conflict = (
            alternative is not None
            and raw_choice is not None
            and alternative.comparison_text != raw_choice.comparison_text
        )
        gamma_override = (
            transform_conflict
            and len(selected_capability_contexts) >= 2
            and selected_capability_stability < self.config.minimum_stability
        )
        if gamma_override:
            selected = alternative
            selected_confidence = alternative_confidence
        reasons: list[str] = []
        nonempty = tuple(item for item in observations if item.comparison_text)
        if self._has_capability_replica_conflict(nonempty):
            reasons.append("capability_replica_conflict")
        if any(item.source_replica_conflict for item in observations):
            if "capability_replica_conflict" not in reasons:
                reasons.append("capability_replica_conflict")
        contexts = {item.context_sha256 for item in nonempty}
        if not nonempty:
            reasons.append("no_observation")
        if raw_choice is None:
            reasons.append("no_raw_control")
        if selected is None:
            reasons.append("unresolved")
        if selected is not None and len(contexts) < 2:
            reasons.append("single_context")
        selected_stability = stability
        if gamma_override:
            selected_stability = 1.0
            reasons.append("gamma_stable_override")
        elif script_confidence_override is not None:
            selected_stability = 1.0
            reasons.append("script_confidence_override")
        elif len(raw) > 1 and stability < self.config.minimum_stability and not exact_context_majority:
            reasons.append("unstable_raw_text")
        if transform_conflict and not gamma_override:
            reasons.append("transform_conflict")
        if selected_confidence is not None and selected_confidence < self.config.minimum_confidence:
            exact_contexts = {
                item.context_sha256
                for item in nonempty
                if selected is not None
                and selected.transform is OcrTransform.RAW
                and item.capability_id == selected.capability_id
                and item.transform is selected.transform
                and item.comparison_text == selected.comparison_text
            }
            stable_exact_context_consensus = (
                len(exact_contexts) >= 2 and selected_capability_stability >= self.config.minimum_stability
            )
            if stable_exact_context_consensus:
                reasons.append("low_confidence_stable_context_consensus")
            else:
                reasons.append("low_confidence")
        alignments = (
            tuple(
                ObservationAlignment(
                    observation.observation_id,
                    budget.align(selected.text, observation.text),
                )
                for observation in observations
            )
            if selected is not None
            else ()
        )
        return SegmentFusion(
            segment_id=segment_id,
            selected_text=selected.text if selected is not None else None,
            selected_observation_id=(selected.observation_id if selected is not None else None),
            selected_transform=selected.transform if selected is not None else None,
            selected_lane_id=selected.lane_id if selected is not None else None,
            confidence=selected_confidence,
            observation_count=len(observations),
            independent_context_count=len(contexts),
            stability=selected_stability,
            uncertainty_reasons=tuple(reasons),
            script_scores=exact_script_scores(selected.text if selected is not None else ""),
            alignments=alignments,
        )

    def _fuse_group(
        self,
        unit: MembershipUnit,
        observations: tuple[SegmentGroupObservation, ...],
        *,
        budget: _AlignmentBudget,
    ) -> SegmentGroupFusion:
        if unit.kind is not MembershipUnitKind.SUBBLOCK:
            raise OcrFusionInvariantError("only a subblock can produce group OCR evidence")
        adapted = tuple(
            SegmentObservation(
                observation_id=item.observation_id,
                job_id=item.job_id,
                segment_id=unit.unit_id,
                block_id=item.block_id,
                transform=item.transform,
                lane_id=item.lane_id,
                capability_id=item.capability_id,
                text=item.text,
                confidence=item.confidence,
                page_bboxes=item.page_bboxes,
                input_sha256=item.input_sha256,
                context_sha256=item.context_sha256,
                source_replica_conflict=item.source_replica_conflict,
            )
            for item in observations
        )
        fused = self._fuse_segment(unit.unit_id, adapted, budget=budget)
        return SegmentGroupFusion(
            unit_id=unit.unit_id,
            segment_ids=unit.segment_ids,
            selected_text=fused.selected_text,
            selected_observation_id=fused.selected_observation_id,
            selected_transform=fused.selected_transform,
            selected_lane_id=fused.selected_lane_id,
            confidence=fused.confidence,
            observation_count=fused.observation_count,
            independent_context_count=fused.independent_context_count,
            stability=fused.stability,
            uncertainty_reasons=fused.uncertainty_reasons,
            script_scores=fused.script_scores,
            alignments=fused.alignments,
        )

    @staticmethod
    def _independent_observations(
        observations: tuple[SegmentObservation, ...],
    ) -> tuple[SegmentObservation, ...]:
        """Give each capability/context/transform source exactly one vote."""

        representatives: dict[tuple[str, str, OcrTransform], tuple[int, SegmentObservation]] = {}
        for index, observation in enumerate(observations):
            key = (
                observation.context_sha256,
                observation.capability_id,
                observation.transform,
            )
            current = representatives.get(key)
            if current is None or observation.confidence > current[1].confidence:
                representatives[key] = (index, observation)
        return tuple(
            item
            for _, item in sorted(
                representatives.values(),
                key=lambda value: value[0],
            )
        )

    @staticmethod
    def _has_capability_replica_conflict(
        observations: tuple[SegmentObservation, ...],
    ) -> bool:
        observed_texts: dict[tuple[str, str, OcrTransform], set[str]] = {}
        for observation in observations:
            key = (
                observation.context_sha256,
                observation.capability_id,
                observation.transform,
            )
            observed_texts.setdefault(key, set()).add(observation.comparison_text)
        return any(len(values) > 1 for values in observed_texts.values())

    def _has_exact_context_majority(
        self,
        observations: tuple[SegmentObservation, ...],
        *,
        selected: SegmentObservation | None,
    ) -> bool:
        """Accept a strong exact medoid majority over low-confidence outliers."""

        if selected is None or len(observations) < 2:
            return False
        if any(item.capability_id != selected.capability_id for item in observations):
            return False
        contexts = {item.context_sha256 for item in observations}
        if len(contexts) != len(observations):
            return False
        agreeing = tuple(item for item in observations if item.comparison_text == selected.comparison_text)
        disagreeing = tuple(item for item in observations if item.comparison_text != selected.comparison_text)
        agreement_fraction = len(agreeing) / len(observations)
        return (
            len(agreeing) > len(disagreeing)
            and agreement_fraction >= self.config.minimum_exact_context_majority_fraction
            and bool(disagreeing)
            and all(item.confidence < self.config.minimum_confidence for item in disagreeing)
        )

    def _observed_medoid(
        self,
        observations: tuple[SegmentObservation, ...],
        budget: _AlignmentBudget,
    ) -> tuple[SegmentObservation | None, float, float]:
        if not observations:
            return None, 0.0, 0.0
        indexed = tuple(enumerate(observations))
        grouped: dict[str, list[tuple[int, SegmentObservation]]] = {}
        for index, observation in indexed:
            grouped.setdefault(observation.comparison_text, []).append((index, observation))
        groups: list[tuple[str, int, int, float, SegmentObservation]] = []
        for comparison_text, members in grouped.items():
            _, representative = max(
                members,
                key=lambda item: (item[1].confidence, -item[0]),
            )
            groups.append(
                (
                    comparison_text,
                    len(members),
                    members[0][0],
                    sum(item.confidence for _, item in members) / len(members),
                    representative,
                )
            )
        candidates: list[tuple[float, int, float, int, SegmentObservation]] = []
        for text, count, first_index, mean_confidence, representative in groups:
            agreement = sum(
                budget.align(text, other_text).similarity * other_count for other_text, other_count, _, _, _ in groups
            ) / len(observations)
            candidates.append(
                (
                    agreement,
                    count,
                    mean_confidence,
                    -first_index,
                    representative,
                )
            )
        winner = max(candidates, key=lambda item: item[:4])
        pair_count = len(observations) * (len(observations) - 1) // 2
        weighted_similarity = sum(count * (count - 1) / 2 for _, count, _, _, _ in groups)
        for index, (text, count, _, _, _) in enumerate(groups):
            for other_text, other_count, _, _, _ in groups[index + 1 :]:
                weighted_similarity += budget.align(text, other_text).similarity * count * other_count
        stability = weighted_similarity / pair_count if pair_count else 0.0
        return winner[4], winner[2], stability

    @staticmethod
    def _script_confidence_override(
        observations: tuple[SegmentObservation, ...],
    ) -> tuple[SegmentObservation, float] | None:
        if len({item.context_sha256 for item in observations}) < 2:
            return None
        minimum_script_density = 0.01
        script_contexts: dict[str, set[str]] = {}
        scripts_by_observation: dict[str, frozenset[str]] = {}
        for observation in observations:
            scripts = frozenset(
                script for script, density in exact_script_scores(observation.text) if density >= minimum_script_density
            )
            scripts_by_observation[observation.observation_id] = scripts
            for script in scripts:
                script_contexts.setdefault(script, set()).add(observation.context_sha256)
        supported_scripts = frozenset(script for script, contexts in script_contexts.items() if len(contexts) >= 2)
        if len(supported_scripts) < 2:
            return None

        grouped: dict[str, list[SegmentObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.comparison_text, []).append(observation)
        visually_confusable_latin = frozenset("ABCEHKMOPTXYabcehkmoptxyl")

        def minority_confusables(text: str) -> int:
            if not any("\u0400" <= character <= "\u04ff" for character in text):
                return 0
            tokens = []
            current = []
            for character in text:
                if "A" <= character <= "Z" or "a" <= character <= "z":
                    current.append(character)
                elif current:
                    tokens.append("".join(current))
                    current = []
            if current:
                tokens.append("".join(current))
            return sum(
                len(token) <= 2 and all(character in visually_confusable_latin for character in token)
                for token in tokens
            )

        candidates = []
        for first_index, members in enumerate(grouped.values()):
            representative = max(
                members,
                key=lambda item: item.confidence,
            )
            mean_confidence = sum(item.confidence for item in members) / len(members)
            coverage = len(scripts_by_observation[representative.observation_id] & supported_scripts)
            candidates.append(
                (
                    coverage,
                    mean_confidence,
                    len({item.context_sha256 for item in members}),
                    -first_index,
                    representative,
                    minority_confusables(representative.text),
                    sum(character in "!?" for character in representative.text),
                )
            )
        minimum_confusables = min(item[5] for item in candidates)
        clean_candidates = tuple(item for item in candidates if item[5] == minimum_confusables)
        if len(clean_candidates) == 1 and any(item[5] > minimum_confusables for item in candidates):
            clean = clean_candidates[0]
            runner_confidence = max(item[1] for item in candidates if item is not clean)
            clean_text = clean[4].comparison_text
            similar_to_runner = any(
                sum(
                    left == right
                    for left, right in zip(
                        clean_text,
                        item[4].comparison_text,
                    )
                )
                / max(1, len(clean_text), len(item[4].comparison_text))
                >= 0.80
                for item in candidates
                if item is not clean
            )
            runner_punctuation = min(item[6] for item in candidates if item is not clean)
            if clean[1] >= runner_confidence - 0.02 and clean[6] <= runner_punctuation and similar_to_runner:
                return clean[4], clean[1]
        candidates.sort(key=lambda item: item[:4], reverse=True)
        winner = candidates[0]
        if winner[0] < 2:
            return None
        exact_contexts = winner[2]
        if exact_contexts < 2:
            return None
        return winner[4], winner[1]

    def _stable_alternative(
        self,
        observations: tuple[SegmentObservation, ...],
        *,
        control: SegmentObservation | None,
        control_confidence: float,
    ) -> tuple[SegmentObservation | None, float]:
        if control is None:
            return None, 0.0
        control_capability = control.capability_id
        same_capability_raw = self._independent_observations(
            tuple(
                observation
                for observation in observations
                if observation.transform is OcrTransform.RAW
                and observation.capability_id == control_capability
                and observation.comparison_text
            )
        )
        comparable_control_confidence = (
            sum(item.confidence for item in same_capability_raw) / len(same_capability_raw)
            if same_capability_raw
            else control_confidence
        )
        by_text: dict[str, list[SegmentObservation]] = {}
        for observation in observations:
            if (
                observation.transform is OcrTransform.GAMMA
                and observation.capability_id == control_capability
                and observation.comparison_text
            ):
                by_text.setdefault(observation.comparison_text, []).append(observation)
        candidates: list[tuple[float, int, int, SegmentObservation]] = []
        observation_order = {observation.observation_id: index for index, observation in enumerate(observations)}
        for text_index, text_observations in enumerate(by_text.values()):
            independent = self._independent_observations(tuple(text_observations))
            bbox_coverage = all(item.page_bboxes for item in independent)
            mean_confidence = sum(item.confidence for item in independent) / len(independent)
            if (
                len(independent) < self.config.minimum_alternative_contexts
                or not bbox_coverage
                or mean_confidence < comparable_control_confidence
            ):
                continue
            representative = max(
                independent,
                key=lambda item: (
                    item.confidence,
                    -observation_order[item.observation_id],
                ),
            )
            candidates.append((mean_confidence, len(independent), -text_index, representative))
        winner = max(
            candidates,
            default=(0.0, 0, 0, None),
            key=lambda item: item[:3],
        )
        return winner[3], winner[0]

    def _overlaps(
        self,
        *,
        plan: BlockPlan,
        observations: tuple[SegmentObservation, ...],
        fusions: tuple[SegmentFusion, ...],
        budget: _AlignmentBudget,
    ) -> tuple[OverlapConsensus, ...]:
        fusion_by_id = {item.segment_id: item for item in fusions}
        observation_by_id = {item.observation_id: item for item in observations}
        observed_keys = {
            (
                observation.block_id,
                observation.segment_id,
                observation.transform,
                observation.capability_id,
            )
            for observation in observations
            if observation.comparison_text
        }
        by_context: dict[tuple[str, str], list[SegmentObservation]] = {}
        for observation in observations:
            by_context.setdefault((observation.block_id, observation.segment_id), []).append(observation)
        values: list[OverlapConsensus] = []
        for algebra in plan.adjacent_algebra:
            confirmed: list[str] = []
            cross_transform_confirmed: list[str] = []
            near_confirmed: list[str] = []
            deferred: list[str] = []
            conflicting: list[str] = []
            missing: list[str] = []
            for segment_id in algebra.intersection_segment_ids:
                fusion = fusion_by_id[segment_id]
                selected = (
                    observation_by_id.get(fusion.selected_observation_id)
                    if fusion.selected_observation_id is not None
                    else None
                )
                if selected is None:
                    missing.append(segment_id)
                    continue

                def selected_evidence(block_id: str) -> tuple[SegmentObservation, ...]:
                    return tuple(
                        item
                        for item in by_context.get((block_id, segment_id), [])
                        if item.comparison_text
                        and item.transform is selected.transform
                        and item.capability_id == selected.capability_id
                    )

                selected_text = compact_ocr_text(fusion.selected_text or "")

                def exact_selected_evidence(
                    block_id: str,
                ) -> tuple[SegmentObservation, ...]:
                    return tuple(
                        item
                        for item in by_context.get((block_id, segment_id), [])
                        if item.capability_id == selected.capability_id
                        and item.comparison_text == selected_text
                        and item.confidence >= self.config.minimum_confidence
                        and item.page_bboxes
                        and not item.source_replica_conflict
                    )

                first_evidence = self._independent_observations(selected_evidence(algebra.first_block_id))
                second_evidence = self._independent_observations(selected_evidence(algebra.second_block_id))
                first_contexts = {item.context_sha256 for item in first_evidence}
                second_contexts = {item.context_sha256 for item in second_evidence}
                if not first_contexts.isdisjoint(second_contexts):
                    missing.append(segment_id)
                    continue
                first_exact = exact_selected_evidence(algebra.first_block_id)
                second_exact = exact_selected_evidence(algebra.second_block_id)
                first_exact_contexts = {item.context_sha256 for item in first_exact}
                second_exact_contexts = {item.context_sha256 for item in second_exact}
                exact_cross_transform = bool(
                    not fusion.unresolved
                    and first_exact_contexts
                    and second_exact_contexts
                    and first_exact_contexts.isdisjoint(second_exact_contexts)
                    and any(item.transform is not selected.transform for item in first_exact + second_exact)
                )
                first, _, _ = self._observed_medoid(
                    first_evidence,
                    budget,
                )
                second, _, _ = self._observed_medoid(
                    second_evidence,
                    budget,
                )
                if first is None or second is None:
                    if exact_cross_transform:
                        cross_transform_confirmed.append(segment_id)
                    else:
                        missing.append(segment_id)
                elif first.comparison_text == second.comparison_text == selected_text:
                    confirmed.append(segment_id)
                else:
                    core_block_ids = tuple(
                        block.block_id for block in plan.blocks if segment_id in block.core_segment_ids
                    )
                    selected_is_unique_core = core_block_ids == (selected.block_id,)
                    core_medoid = (
                        first
                        if selected.block_id == algebra.first_block_id
                        else second if selected.block_id == algebra.second_block_id else None
                    )
                    near_core_consensus = (
                        not fusion.unresolved
                        and selected.transform is OcrTransform.RAW
                        and selected_is_unique_core
                        and core_medoid is not None
                        and core_medoid.comparison_text == selected_text
                        and fusion.confidence is not None
                        and fusion.confidence >= self.config.minimum_confidence
                        and first.confidence >= self.config.minimum_confidence
                        and second.confidence >= self.config.minimum_confidence
                        and first.page_bboxes
                        and first.page_bboxes == second.page_bboxes
                        and not first.source_replica_conflict
                        and not second.source_replica_conflict
                        and budget.align(
                            first.comparison_text,
                            second.comparison_text,
                        ).similarity
                        >= self.config.minimum_stability
                    )
                    if exact_cross_transform:
                        cross_transform_confirmed.append(segment_id)
                    elif near_core_consensus:
                        near_confirmed.append(segment_id)
                    elif fusion.unresolved:
                        deferred.append(segment_id)
                    else:
                        conflicting.append(segment_id)
            pair_block_ids = {algebra.first_block_id, algebra.second_block_id}
            pair_observed_segment_ids: set[str] = set()
            for segment_id in algebra.union_segment_ids:
                fusion = fusion_by_id[segment_id]
                selected = (
                    observation_by_id.get(fusion.selected_observation_id)
                    if fusion.selected_observation_id is not None
                    else None
                )
                if selected is not None and any(
                    (
                        block_id,
                        segment_id,
                        selected.transform,
                        selected.capability_id,
                    )
                    in observed_keys
                    for block_id in pair_block_ids
                ):
                    pair_observed_segment_ids.add(segment_id)
            observed_union = tuple(
                segment_id for segment_id in algebra.union_segment_ids if segment_id in pair_observed_segment_ids
            )
            observed_xor = tuple(
                segment_id for segment_id in algebra.xor_segment_ids if segment_id in pair_observed_segment_ids
            )
            values.append(
                OverlapConsensus(
                    first_block_id=algebra.first_block_id,
                    second_block_id=algebra.second_block_id,
                    intersection_segment_ids=algebra.intersection_segment_ids,
                    union_segment_ids=algebra.union_segment_ids,
                    xor_segment_ids=algebra.xor_segment_ids,
                    confirmed_intersection_segment_ids=tuple(confirmed),
                    cross_transform_confirmed_intersection_segment_ids=tuple(cross_transform_confirmed),
                    near_confirmed_intersection_segment_ids=tuple(near_confirmed),
                    deferred_intersection_segment_ids=tuple(deferred),
                    conflicting_intersection_segment_ids=tuple(conflicting),
                    missing_intersection_segment_ids=tuple(missing),
                    observed_union_segment_ids=observed_union,
                    observed_xor_segment_ids=observed_xor,
                )
            )
        return tuple(values)


_SCRIPT_MARKERS = (
    ("CYRILLIC", "cyrillic"),
    ("LATIN", "latin"),
    ("GREEK", "greek"),
    ("CJK", "cjk"),
    ("IDEOGRAPH", "cjk"),
    ("HIRAGANA", "japanese"),
    ("KATAKANA", "japanese"),
    ("HANGUL", "korean"),
    ("ARABIC", "arabic"),
    ("HEBREW", "hebrew"),
    ("DEVANAGARI", "devanagari"),
)


def exact_script_scores(value: str) -> tuple[tuple[str, float], ...]:
    counts: dict[str, int] = {}
    for character in value:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        script = next(
            (script for marker, script in _SCRIPT_MARKERS if marker in name),
            None,
        )
        if script is not None:
            counts[script] = counts.get(script, 0) + 1
    total = sum(counts.values())
    if not total:
        return ()
    return tuple((script, count / total) for script, count in sorted(counts.items()))


__all__ = [
    "BlockTextObservation",
    "EditKind",
    "EditOperation",
    "ExactTextAlignment",
    "ObservationAlignment",
    "OcrEvidenceFusion",
    "OcrFusionConfig",
    "OcrFusionInvariantError",
    "OcrFusionLimitError",
    "OcrFusionResult",
    "OcrFusionStatus",
    "OcrRoutingMode",
    "OverlapConsensus",
    "OcrReplicaConflict",
    "SegmentFusion",
    "SegmentGroupFusion",
    "SegmentGroupObservation",
    "SegmentObservation",
    "UnassignedWordObservation",
    "align_exact_text",
    "compact_ocr_text",
    "exact_script_scores",
]
