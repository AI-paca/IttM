from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningConfig,
    OverlappingBlockPlanner,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import (
    GeometryResult,
    GeometryStatus,
    SegmentSpan,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.object_reconstruction import (
    DocumentObject,
    ObjectKind,
    ObjectReconstructionConfig,
    ObjectReconstructionResult,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_fusion import (
    BlockTextObservation,
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionResult,
    SegmentFusion,
    SegmentGroupFusion,
    SegmentGroupObservation,
    SegmentObservation,
    compact_ocr_text,
)
from app.sparse_pipeline.ocr_queue import (
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrQueueStatus,
    OcrTransform,
)


class AssemblyStatus(str, Enum):
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"


class AttributionLevel(str, Enum):
    SEGMENT = "segment"
    SEGMENT_GROUP = "segment_group"
    OBJECT = "object"
    UNATTRIBUTABLE = "unattributable"


class StructuralUnitKind(str, Enum):
    PARAGRAPH_LINE = "paragraph_line"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    UNKNOWN_FRAGMENT = "unknown_fragment"


class DocumentAssemblyInvariantError(ValueError):
    """Raised when evidence from stages 1/6/5/2 is not the same run."""


class DocumentAssemblyLimitError(RuntimeError):
    """Raised before document assembly would exceed an explicit work bound."""


def _markdown_for_object(kind: ObjectKind, text: str) -> str:
    if kind is not ObjectKind.TABLE or not text:
        return text
    longest_backticks = 0
    longest_tildes = 0
    current_backticks = 0
    current_tildes = 0
    for character in text:
        current_backticks = current_backticks + 1 if character == "`" else 0
        current_tildes = current_tildes + 1 if character == "~" else 0
        longest_backticks = max(longest_backticks, current_backticks)
        longest_tildes = max(longest_tildes, current_tildes)
    marker, longest = min(
        (("`", longest_backticks), ("~", longest_tildes)),
        key=lambda item: (item[1], item[0]),
    )
    fence = marker * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


@dataclass(frozen=True)
class DocumentAssemblyConfig:
    max_segments: int = 100_000
    max_objects: int = 100_000
    max_blocks: int = 100_000
    max_jobs: int = 100_000
    max_structural_units: int = 1_000_000
    max_evidence_slices: int = 2_000_000
    max_text_characters: int = 16_000_000
    max_markdown_characters: int = 32_000_000
    max_anchor_search_characters: int = 64_000_000
    max_crop_bytes: int = 64 * 1024 * 1024
    max_total_crop_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_segments,
            self.max_objects,
            self.max_blocks,
            self.max_jobs,
            self.max_structural_units,
            self.max_evidence_slices,
            self.max_text_characters,
            self.max_markdown_characters,
            self.max_anchor_search_characters,
            self.max_crop_bytes,
            self.max_total_crop_bytes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("document assembly limits must be positive integers")


@dataclass(frozen=True)
class EvidenceSlice:
    slice_id: str
    job_id: str
    observation_id: str | None
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    input_sha256: str
    context_sha256: str
    object_id: str | None
    segment_ids: tuple[str, ...]
    attribution_level: AttributionLevel
    text: str
    output_start: int
    output_stop: int

    def __post_init__(self) -> None:
        identifiers = (
            self.slice_id,
            self.job_id,
            self.block_id,
            self.lane_id,
            self.capability_id,
        )
        if any(type(value) is not str or not value for value in identifiers):
            raise ValueError("evidence slice identifiers must not be empty")
        if self.observation_id is not None and (
            type(self.observation_id) is not str or not self.observation_id
        ):
            raise ValueError("evidence observation identifier is invalid")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("evidence transform is invalid")
        if not isinstance(self.attribution_level, AttributionLevel):
            raise ValueError("evidence attribution level is invalid")
        if self.attribution_level is AttributionLevel.UNATTRIBUTABLE:
            if self.object_id is not None:
                raise ValueError("unattributable evidence cannot claim an object")
        elif type(self.object_id) is not str or not self.object_id:
            raise ValueError("attributable evidence must name one object")
        if (
            type(self.segment_ids) is not tuple
            or not self.segment_ids
            or len(self.segment_ids) != len(set(self.segment_ids))
            or any(type(value) is not str or not value for value in self.segment_ids)
        ):
            raise ValueError("evidence segment identifiers are invalid")
        if type(self.text) is not str or not compact_ocr_text(self.text):
            raise ValueError("an evidence slice must contain observed codepoints")
        if (
            type(self.output_start) is not int
            or type(self.output_stop) is not int
            or self.output_start < 0
            or self.output_stop <= self.output_start
        ):
            raise ValueError("evidence output offsets are invalid")
        for digest in (self.input_sha256, self.context_sha256):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(value not in "0123456789abcdef" for value in digest)
            ):
                raise ValueError("evidence digest is invalid")


@dataclass(frozen=True)
class SegmentTextAssembly:
    segment_id: str
    object_id: str
    candidate_text: str
    text: str | None
    attribution_level: AttributionLevel
    selected_observation_id: str | None
    evidence_slice_ids: tuple[str, ...]
    status: AssemblyStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.segment_id or not self.object_id:
            raise ValueError("segment assembly identifiers must not be empty")
        if type(self.candidate_text) is not str:
            raise ValueError("segment candidate text must be a string")
        if self.text is not None and self.text != self.candidate_text:
            raise ValueError("certified segment text must equal its candidate")
        expected = (
            AssemblyStatus.COMPLETE
            if self.text is not None
            else AssemblyStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("segment assembly status disagrees with text")
        if self.attribution_level is not AttributionLevel.SEGMENT:
            raise ValueError("segment assembly attribution must be SEGMENT")


@dataclass(frozen=True)
class StructuralUnit:
    unit_id: str
    object_id: str
    kind: ObjectKind
    unit_kind: StructuralUnitKind
    segment_ids: tuple[str, ...]
    row_start: int
    row_stop: int
    column_start: int
    column_stop: int
    candidate_text: str
    text: str | None
    evidence_slice_ids: tuple[str, ...]
    status: AssemblyStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.unit_id or not self.object_id:
            raise ValueError("structural unit identifiers must not be empty")
        if not isinstance(self.kind, ObjectKind):
            raise ValueError("structural unit kind is invalid")
        if not isinstance(self.unit_kind, StructuralUnitKind):
            raise ValueError("structural unit type is invalid")
        if (
            type(self.segment_ids) is not tuple
            or any(type(item) is not str or not item for item in self.segment_ids)
            or len(self.segment_ids) != len(set(self.segment_ids))
        ):
            raise ValueError("structural unit must own unique segments")
        if not self.segment_ids and self.unit_kind is not StructuralUnitKind.TABLE_CELL:
            raise ValueError("only a table cell may be structurally empty")
        if min(self.row_start, self.column_start) < 0 or (
            self.row_stop <= self.row_start
            or self.column_stop <= self.column_start
        ):
            raise ValueError("structural unit sparse span is invalid")
        if type(self.candidate_text) is not str:
            raise ValueError("structural unit candidate must be a string")
        if self.text is not None and self.text != self.candidate_text:
            raise ValueError("certified unit text must equal its candidate")
        expected = (
            AssemblyStatus.COMPLETE
            if self.text is not None
            else AssemblyStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("structural unit status disagrees with text")


@dataclass(frozen=True)
class ObjectTextAssembly:
    object_id: str
    kind: ObjectKind
    segment_ids: tuple[str, ...]
    attribution_level: AttributionLevel
    candidate_text: str
    text: str | None
    candidate_markdown: str
    markdown: str | None
    structural_unit_ids: tuple[str, ...]
    evidence_slice_ids: tuple[str, ...]
    status: AssemblyStatus
    table_row_indices: tuple[int, ...] = ()
    table_column_indices: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.object_id or not self.segment_ids:
            raise ValueError("object assembly identifiers must not be empty")
        if not isinstance(self.kind, ObjectKind):
            raise ValueError("object assembly kind is invalid")
        if not isinstance(self.attribution_level, AttributionLevel):
            raise ValueError("object attribution level is invalid")
        for name, indices in (
            ("row", self.table_row_indices),
            ("column", self.table_column_indices),
        ):
            if (
                type(indices) is not tuple
                or any(type(item) is not int or item < 0 for item in indices)
                or indices != tuple(sorted(set(indices)))
            ):
                raise ValueError(f"table {name} indices are invalid")
        if self.kind is ObjectKind.TABLE:
            if not self.table_row_indices or not self.table_column_indices:
                raise ValueError("a table object must preserve both logical axes")
        elif self.table_row_indices or self.table_column_indices:
            raise ValueError("only a table object may preserve logical axes")
        if self.text is not None and self.text != self.candidate_text:
            raise ValueError("certified object text must equal its candidate")
        if self.kind is not ObjectKind.TABLE:
            expected_markdown = _markdown_for_object(self.kind, self.candidate_text)
            if self.candidate_markdown != expected_markdown:
                raise ValueError("object Markdown was not derived from object text")
            if self.markdown is not None and self.markdown != expected_markdown:
                raise ValueError("certified object Markdown disagrees with object text")
        elif self.markdown is not None and self.markdown != self.candidate_markdown:
            raise ValueError("certified table Markdown disagrees with its candidate")
        if (self.text is None) != (self.markdown is None):
            raise ValueError("object text and Markdown certification disagree")
        expected = (
            AssemblyStatus.COMPLETE
            if self.text is not None
            else AssemblyStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("object assembly status disagrees with text")


@dataclass(frozen=True)
class DocumentAssemblyResult:
    source_segment_ids: tuple[str, ...]
    source_object_ids: tuple[str, ...]
    source_block_ids: tuple[str, ...]
    evidence_slices: tuple[EvidenceSlice, ...]
    segments: tuple[SegmentTextAssembly, ...]
    structural_units: tuple[StructuralUnit, ...]
    objects: tuple[ObjectTextAssembly, ...]
    candidate_text: str
    text: str | None
    candidate_markdown: str
    markdown: str | None
    status: AssemblyStatus
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if tuple(item.segment_id for item in self.segments) != self.source_segment_ids:
            raise ValueError("segment assemblies must follow canonical source order")
        if tuple(item.object_id for item in self.objects) != self.source_object_ids:
            raise ValueError("object assemblies must follow reading order")
        if self.source_block_ids != tuple(
            f"block-{index:06d}" for index in range(len(self.source_block_ids))
        ):
            raise ValueError("source block identifiers must be canonical")
        if tuple(item.slice_id for item in self.evidence_slices) != tuple(
            f"evidence-{index:08d}" for index in range(len(self.evidence_slices))
        ):
            raise ValueError("evidence slice identifiers must be canonical")
        if tuple(item.unit_id for item in self.structural_units) != tuple(
            f"unit-{index:08d}" for index in range(len(self.structural_units))
        ):
            raise ValueError("structural unit identifiers must be canonical")
        if self.text is not None and self.text != self.candidate_text:
            raise ValueError("certified document text must equal its candidate")
        if (self.text is None) != (self.markdown is None):
            raise ValueError("document text and Markdown certification disagree")
        expected = (
            AssemblyStatus.COMPLETE
            if self.text is not None
            else AssemblyStatus.UNRESOLVED
        )
        if self.status is not expected:
            raise ValueError("document assembly status disagrees with text")
        object_candidates = tuple(item.candidate_text for item in self.objects)
        if self.objects and any(object_candidates):
            expected_candidate = "\n\n".join(object_candidates)
            if self.candidate_text != expected_candidate:
                raise ValueError("document candidate disagrees with object order")
            expected_markdown = "\n\n".join(
                item.candidate_markdown for item in self.objects
            )
        elif not self.objects:
            expected_candidate = ""
            expected_markdown = ""
            if self.candidate_text != expected_candidate:
                raise ValueError("empty document cannot contain candidate text")
        else:
            expected_markdown = self.candidate_text
        if self.candidate_markdown != expected_markdown:
            raise ValueError("document Markdown disagrees with assembled objects")
        if self.markdown is not None and self.markdown != expected_markdown:
            raise ValueError("certified document Markdown disagrees with text")

        source_segments = set(self.source_segment_ids)
        source_objects = set(self.source_object_ids)
        source_blocks = set(self.source_block_ids)
        evidence_ids = {item.slice_id for item in self.evidence_slices}
        evidence_by_id = {item.slice_id: item for item in self.evidence_slices}
        unit_ids = {item.unit_id for item in self.structural_units}
        object_segments = {item.object_id: set(item.segment_ids) for item in self.objects}
        if set(object_segments) != source_objects:
            raise ValueError("object records disagree with source object IDs")
        flattened_segments = tuple(
            segment_id for item in self.objects for segment_id in item.segment_ids
        )
        if len(flattened_segments) != len(set(flattened_segments)) or set(
            flattened_segments
        ) != source_segments:
            raise ValueError("object records must exactly partition source segments")
        for item in self.evidence_slices:
            if item.block_id not in source_blocks:
                raise ValueError("evidence slice references foreign source records")
            if item.attribution_level is AttributionLevel.UNATTRIBUTABLE:
                if not set(item.segment_ids).issubset(source_segments):
                    raise ValueError(
                        "unattributable evidence references foreign segments"
                    )
            elif (
                item.object_id not in source_objects
                or not set(item.segment_ids).issubset(
                    object_segments[item.object_id]
                )
            ):
                raise ValueError("evidence slice references foreign source records")
        units_by_object: dict[str, list[StructuralUnit]] = {
            item: [] for item in self.source_object_ids
        }
        for item in self.structural_units:
            if (
                item.object_id not in source_objects
                or not set(item.segment_ids).issubset(object_segments[item.object_id])
                or not set(item.evidence_slice_ids).issubset(evidence_ids)
            ):
                raise ValueError("structural unit references foreign records")
            if any(
                evidence_by_id[slice_id].object_id != item.object_id
                for slice_id in item.evidence_slice_ids
            ):
                raise ValueError("structural unit references another object's evidence")
            units_by_object[item.object_id].append(item)
        segment_by_id = {item.segment_id: item for item in self.segments}
        for segment_id, item in segment_by_id.items():
            if (
                segment_id not in source_segments
                or item.object_id not in source_objects
                or segment_id not in object_segments[item.object_id]
                or not set(item.evidence_slice_ids).issubset(evidence_ids)
            ):
                raise ValueError("segment assembly references foreign records")
            if any(
                evidence_by_id[slice_id].object_id != item.object_id
                or segment_id not in evidence_by_id[slice_id].segment_ids
                for slice_id in item.evidence_slice_ids
            ):
                raise ValueError("segment assembly references unrelated evidence")
        referenced_evidence: set[str] = {
            item.slice_id
            for item in self.evidence_slices
            if item.attribution_level is AttributionLevel.UNATTRIBUTABLE
        }
        for item in self.objects:
            object_units = tuple(units_by_object[item.object_id])
            expected_units = tuple(unit.unit_id for unit in object_units)
            if item.structural_unit_ids != expected_units:
                raise ValueError("object structural units are incomplete or reordered")
            if not set(item.evidence_slice_ids).issubset(evidence_ids):
                raise ValueError("object references foreign evidence")
            if any(
                evidence_by_id[slice_id].object_id != item.object_id
                for slice_id in item.evidence_slice_ids
            ):
                raise ValueError("object references another object's evidence")
            unit_segment_ids = tuple(
                segment_id
                for unit in object_units
                for segment_id in unit.segment_ids
            )
            if (
                len(unit_segment_ids) != len(set(unit_segment_ids))
                or set(unit_segment_ids) != set(item.segment_ids)
            ):
                raise ValueError(
                    "structural units must exactly partition object segments"
                )
            expected_object_markdown = DocumentAssembler._object_markdown(
                item.kind,
                item.candidate_text,
                object_units,
                table_row_indices=item.table_row_indices,
                table_column_indices=item.table_column_indices,
            )
            if item.candidate_markdown != expected_object_markdown:
                raise ValueError("object Markdown disagrees with structural units")
            referenced_evidence.update(item.evidence_slice_ids)
        referenced_evidence.update(
            slice_id for item in self.segments for slice_id in item.evidence_slice_ids
        )
        if referenced_evidence != evidence_ids:
            raise ValueError("document contains orphaned evidence slices")
        if unit_ids != {
            unit_id for item in self.objects for unit_id in item.structural_unit_ids
        }:
            raise ValueError("document contains orphaned structural units")
        for item in self.evidence_slices:
            if item.output_stop > len(self.candidate_text):
                raise ValueError("evidence slice lies outside document output")
        compact_output_cache: dict[tuple[int, int], str] = {}
        compact_evidence_cache: dict[str, str] = {}
        for item in self.evidence_slices:
            output_key = (item.output_start, item.output_stop)
            compact_output = compact_output_cache.get(output_key)
            if compact_output is None:
                compact_output = compact_ocr_text(
                    self.candidate_text[item.output_start : item.output_stop]
                )
                compact_output_cache[output_key] = compact_output
            compact_evidence = compact_evidence_cache.get(item.text)
            if compact_evidence is None:
                compact_evidence = compact_ocr_text(item.text)
                compact_evidence_cache[item.text] = compact_evidence
            if compact_output != compact_evidence:
                raise ValueError("evidence slice does not match document output")
        if self.status is AssemblyStatus.COMPLETE:
            coverage_delta = [0] * (len(self.candidate_text) + 1)
            for item in self.evidence_slices:
                coverage_delta[item.output_start] += 1
                coverage_delta[item.output_stop] -= 1
            active = 0
            for index, character in enumerate(self.candidate_text):
                active += coverage_delta[index]
                if not character.isspace() and active < 1:
                    raise ValueError(
                        "certified output contains codepoints without evidence"
                    )


@dataclass(frozen=True)
class _EvidenceDraft:
    job_id: str
    observation_id: str | None
    block_id: str
    transform: OcrTransform
    lane_id: str
    capability_id: str
    input_sha256: str
    context_sha256: str
    object_id: str | None
    segment_ids: tuple[str, ...]
    attribution_level: AttributionLevel
    text: str


@dataclass(frozen=True)
class _Placement:
    draft: _EvidenceDraft
    start: int
    stop: int


@dataclass(frozen=True)
class _RenderedObject:
    candidate_text: str
    certified: bool
    attribution_level: AttributionLevel
    placements: tuple[_Placement, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _BlockDecision:
    block: RecognitionBlock
    candidate: BlockTextObservation | None
    stable: BlockTextObservation | None
    certified: BlockTextObservation | None
    corroborating: tuple[BlockTextObservation, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _GroupRecovery:
    object_id: str
    segment_ids: tuple[str, ...]
    candidate_text: str
    placements: tuple[_Placement, ...]
    pair: tuple[str, str]
    certified: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _MembershipGroupRecovery:
    object_id: str
    unit_id: str
    segment_ids: tuple[str, ...]
    candidate_text: str
    placements: tuple[_Placement, ...]
    certified: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _StructuralDraft:
    unit_kind: StructuralUnitKind
    segment_ids: tuple[str, ...]
    row_start: int
    row_stop: int
    column_start: int
    column_stop: int


class DocumentAssembler:
    """Assemble object-scoped text without rewriting observed OCR evidence."""

    def __init__(self, config: DocumentAssemblyConfig | None = None) -> None:
        if config is not None and not isinstance(config, DocumentAssemblyConfig):
            raise TypeError("config must be a DocumentAssemblyConfig")
        self.config = config or DocumentAssemblyConfig()

    def assemble(
        self,
        *,
        page: CropInput,
        geometry: GeometryResult,
        objects: ObjectReconstructionResult,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        ownership: np.ndarray | None = None,
        ownership_segment_ids: tuple[str, ...] | None = None,
        object_config: ObjectReconstructionConfig | None = None,
        planning_config: BlockPlanningConfig | None = None,
        crop_config: BlockCropConfig | None = None,
        fusion_config: OcrFusionConfig | None = None,
    ) -> DocumentAssemblyResult:
        self._validate_types(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            ownership=ownership,
            ownership_segment_ids=ownership_segment_ids,
        )
        self._check_limits(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
        )
        self._validate_provenance(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            ownership=ownership,
            ownership_segment_ids=ownership_segment_ids,
            object_config=object_config,
            planning_config=planning_config,
            crop_config=crop_config,
            fusion_config=fusion_config,
        )

        owner_by_segment = {
            item.segment_id: item.object_id for item in objects.segment_ownership
        }
        span_by_id = {item.segment_id: item for item in geometry.matrix.spans}
        coordinates_by_segment: dict[str, list[tuple[int, int]]] = {
            item: [] for item in plan.source_segment_ids
        }
        for cell in geometry.matrix.cells:
            coordinates_by_segment[cell.segment_id].append(
                (cell.row, cell.column)
            )
        sparse_coordinates_by_segment = {
            segment_id: tuple(coordinates)
            for segment_id, coordinates in coordinates_by_segment.items()
        }
        observation_by_id = {
            item.observation_id: item for item in fusion.observations
        }
        group_observation_by_id = {
            item.observation_id: item for item in fusion.group_observations
        }
        job_by_id = {item.job_id: item for item in queue.jobs}
        fused_by_id = {item.segment_id: item for item in fusion.segments}
        segment_values = self._segment_assemblies(
            objects=objects,
            fusion=fusion,
            owner_by_segment=owner_by_segment,
        )
        segment_assembly_by_id = {
            item.segment_id: item for item in segment_values
        }
        block_decisions = self._block_decisions(
            plan=plan,
            queue=queue,
            fusion=fusion,
        )
        direct_decisions_by_segments: dict[
            tuple[str, ...], list[_BlockDecision]
        ] = {}
        for decision in block_decisions:
            if decision.candidate is not None:
                direct_decisions_by_segments.setdefault(
                    decision.block.segment_ids,
                    [],
                ).append(decision)
        group_recoveries, recovered_overlap_pairs = self._recover_segment_groups(
            plan=plan,
            objects=objects,
            block_decisions=block_decisions,
            owner_by_segment=owner_by_segment,
        )
        membership_group_recoveries = self._membership_group_recoveries(
            fusion=fusion,
            owner_by_segment=owner_by_segment,
            observation_by_id=group_observation_by_id,
        )
        structural_drafts_by_object = {
            document_object.object_id: self._structural_drafts(
                document_object=document_object,
                span_by_id=span_by_id,
                matrix=geometry.matrix,
                coordinates_by_segment=sparse_coordinates_by_segment,
            )
            for document_object in objects.objects
        }

        rendered: list[_RenderedObject] = []
        for document_object in objects.objects:
            value = self._render_object(
                document_object=document_object,
                span_by_id=span_by_id,
                segment_assembly_by_id=segment_assembly_by_id,
                fused_by_id=fused_by_id,
                observation_by_id=observation_by_id,
                job_by_id=job_by_id,
                block_decisions=tuple(
                    direct_decisions_by_segments.get(
                        document_object.segment_ids,
                        (),
                    )
                ),
                group_recovery=group_recoveries.get(document_object.object_id),
                membership_groups=membership_group_recoveries.get(
                    document_object.object_id,
                    (),
                ),
                structural_drafts=structural_drafts_by_object[
                    document_object.object_id
                ],
            )
            rendered.append(value)

        global_reasons = self._global_reasons(
            geometry=geometry,
            queue=queue,
            fusion=fusion,
            block_decisions=block_decisions,
            recovered_overlap_pairs=recovered_overlap_pairs,
        )
        all_objects_certified = all(item.certified for item in rendered)
        document_certified = all_objects_certified and not global_reasons

        object_texts = tuple(item.candidate_text for item in rendered)
        fallback_decisions: tuple[_BlockDecision, ...] = ()
        if objects.objects and any(object_texts):
            projected_text_characters = sum(map(len, object_texts)) + 2 * (
                len(object_texts) - 1
            )
            if projected_text_characters > self.config.max_text_characters:
                raise DocumentAssemblyLimitError(
                    "assembled output exceeds configured character limit"
                )
            candidate_text, object_offsets = self._join_with_offsets(
                object_texts,
                separator="\n\n",
            )
        elif not objects.objects:
            candidate_text, object_offsets = "", ()
        else:
            fallback_decisions = tuple(
                item
                for item in block_decisions
                if item.candidate is not None
            )
            fallback = tuple(
                item.candidate.text
                for item in fallback_decisions
                if item.candidate is not None
            )
            candidate_text = "\n".join(dict.fromkeys(fallback))
            object_offsets = tuple((0, 0) for _ in objects.objects)
            document_certified = False
            global_reasons = tuple(
                dict.fromkeys(global_reasons + ("unattributable-block-text",))
            )

        if len(candidate_text) > self.config.max_text_characters:
            raise DocumentAssemblyLimitError(
                "assembled output exceeds configured character limit"
            )

        evidence_slices, slice_ids_by_object = self._finalize_evidence(
            rendered=tuple(rendered),
            object_offsets=object_offsets,
            candidate_text=candidate_text,
        )
        if fallback_decisions:
            evidence_slices += self._fallback_evidence_slices(
                decisions=fallback_decisions,
                candidate_text=candidate_text,
                first_index=len(evidence_slices),
            )
        if len(evidence_slices) > self.config.max_evidence_slices:
            raise DocumentAssemblyLimitError(
                "evidence slice count exceeds configured limit"
            )

        segment_evidence_ids: dict[tuple[str, str], list[str]] = {}
        group_evidence_ids: dict[
            tuple[str, tuple[str, ...]], list[str]
        ] = {}
        object_evidence_ids: dict[str, list[str]] = {}
        evidence_by_id = {item.slice_id: item for item in evidence_slices}
        for item in evidence_slices:
            if item.object_id is None:
                continue
            if item.attribution_level is AttributionLevel.SEGMENT:
                for segment_id in item.segment_ids:
                    segment_evidence_ids.setdefault(
                        (item.object_id, segment_id),
                        [],
                    ).append(item.slice_id)
            elif item.attribution_level is AttributionLevel.SEGMENT_GROUP:
                group_evidence_ids.setdefault(
                    (item.object_id, item.segment_ids),
                    [],
                ).append(item.slice_id)
            elif item.attribution_level is AttributionLevel.OBJECT:
                object_evidence_ids.setdefault(item.object_id, []).append(
                    item.slice_id
                )

        structural_units: list[StructuralUnit] = []
        object_values: list[ObjectTextAssembly] = []
        for index, (document_object, value) in enumerate(
            zip(objects.objects, rendered)
        ):
            object_certified = value.certified and not global_reasons
            combined_reasons = tuple(
                dict.fromkeys(value.reasons + global_reasons)
            )
            drafts = structural_drafts_by_object[document_object.object_id]
            whole_object_group = (
                document_object.kind is not ObjectKind.TABLE
                and value.attribution_level is AttributionLevel.SEGMENT_GROUP
                and bool(
                    group_evidence_ids.get(
                        (
                            document_object.object_id,
                            document_object.segment_ids,
                        ),
                        (),
                    )
                )
            )
            if whole_object_group and drafts:
                drafts = (
                    _StructuralDraft(
                        unit_kind=drafts[0].unit_kind,
                        segment_ids=document_object.segment_ids,
                        row_start=document_object.row_start,
                        row_stop=document_object.row_stop,
                        column_start=document_object.column_start,
                        column_stop=document_object.column_stop,
                    ),
                )
            if (
                len(structural_units) + len(drafts)
                > self.config.max_structural_units
            ):
                raise DocumentAssemblyLimitError(
                    "structural unit count exceeds configured limit"
                )
            object_unit_ids: list[str] = []
            object_units: list[StructuralUnit] = []
            for draft in drafts:
                unit_id = f"unit-{len(structural_units):08d}"
                object_unit_ids.append(unit_id)
                whole_object_evidence = (
                    draft.segment_ids == document_object.segment_ids
                    and value.attribution_level
                    in {AttributionLevel.OBJECT, AttributionLevel.SEGMENT_GROUP}
                )
                matching_group_ids = tuple(
                    group_evidence_ids.get(
                        (document_object.object_id, draft.segment_ids),
                        (),
                    )
                )
                if matching_group_ids:
                    group_texts = tuple(
                        evidence_by_id[item].text for item in matching_group_ids
                    )
                    compact_group_texts = {
                        compact_ocr_text(item) for item in group_texts
                    }
                    unit_candidate = group_texts[0]
                    unit_certified = (
                        object_certified and len(compact_group_texts) == 1
                    )
                elif whole_object_evidence:
                    unit_candidate = value.candidate_text
                    unit_certified = object_certified
                else:
                    unit_segments = tuple(
                        segment_assembly_by_id[item]
                        for item in draft.segment_ids
                    )
                    unit_candidate = " ".join(
                        item.candidate_text
                        for item in unit_segments
                        if item.candidate_text
                    )
                    unit_certified = (
                        not global_reasons
                        and all(
                            item.status is AssemblyStatus.COMPLETE
                            for item in unit_segments
                        )
                        and (bool(unit_segments) or not draft.segment_ids)
                    )
                unit_evidence_ids = {
                    slice_id
                    for segment_id in draft.segment_ids
                    for slice_id in segment_evidence_ids.get(
                        (document_object.object_id, segment_id),
                        (),
                    )
                }
                unit_evidence_ids.update(
                    group_evidence_ids.get(
                        (document_object.object_id, draft.segment_ids),
                        (),
                    )
                )
                if draft.segment_ids == document_object.segment_ids:
                    unit_evidence_ids.update(
                        object_evidence_ids.get(
                            document_object.object_id,
                            (),
                        )
                    )
                unit_slice_ids = tuple(sorted(unit_evidence_ids))
                unit_text = unit_candidate if unit_certified else None
                unit = StructuralUnit(
                    unit_id=unit_id,
                    object_id=document_object.object_id,
                    kind=document_object.kind,
                    unit_kind=draft.unit_kind,
                    segment_ids=draft.segment_ids,
                    row_start=draft.row_start,
                    row_stop=draft.row_stop,
                    column_start=draft.column_start,
                    column_stop=draft.column_stop,
                    candidate_text=unit_candidate,
                    text=unit_text,
                    evidence_slice_ids=unit_slice_ids,
                    status=(
                        AssemblyStatus.COMPLETE
                        if unit_text is not None
                        else AssemblyStatus.UNRESOLVED
                    ),
                    reasons=combined_reasons,
                )
                structural_units.append(unit)
                object_units.append(unit)
                table_row_indices, table_column_indices = self._table_axes(
                    document_object=document_object,
                    matrix=geometry.matrix,
                )
            candidate_markdown = self._object_markdown(
                document_object.kind,
                value.candidate_text,
                tuple(object_units),
                table_row_indices=table_row_indices,
                table_column_indices=table_column_indices,
            )
            object_values.append(
                ObjectTextAssembly(
                    object_id=document_object.object_id,
                    kind=document_object.kind,
                    segment_ids=document_object.segment_ids,
                    attribution_level=value.attribution_level,
                    candidate_text=value.candidate_text,
                    text=value.candidate_text if object_certified else None,
                    candidate_markdown=candidate_markdown,
                    markdown=candidate_markdown if object_certified else None,
                    structural_unit_ids=tuple(object_unit_ids),
                    evidence_slice_ids=slice_ids_by_object[index],
                    status=(
                        AssemblyStatus.COMPLETE
                        if object_certified
                        else AssemblyStatus.UNRESOLVED
                    ),
                    table_row_indices=table_row_indices,
                    table_column_indices=table_column_indices,
                    reasons=combined_reasons,
                )
            )

        segment_slice_ids: dict[str, list[str]] = {
            item: [] for item in plan.source_segment_ids
        }
        for item in evidence_slices:
            if item.attribution_level is AttributionLevel.SEGMENT:
                for segment_id in item.segment_ids:
                    segment_slice_ids[segment_id].append(item.slice_id)
        segment_values = tuple(
            replace(
                item,
                evidence_slice_ids=tuple(segment_slice_ids[item.segment_id]),
            )
            for item in segment_values
        )
        candidate_markdown = (
            "\n\n".join(item.candidate_markdown for item in object_values)
            if any(object_texts)
            else candidate_text
        )
        if len(candidate_markdown) > self.config.max_markdown_characters:
            raise DocumentAssemblyLimitError(
                "assembled Markdown exceeds configured character limit"
            )
        diagnostics = (
            "assembly=object-scoped",
            "reference-evidence=forbidden",
            "non-whitespace-output=observed-only",
            "context-ownership=stage6-segment-map",
            "text-only-splitting=fail-closed",
            f"global-blockers={len(global_reasons)}",
            *global_reasons,
        )
        return DocumentAssemblyResult(
            source_segment_ids=plan.source_segment_ids,
            source_object_ids=tuple(item.object_id for item in objects.objects),
            source_block_ids=tuple(item.block_id for item in plan.blocks),
            evidence_slices=evidence_slices,
            segments=segment_values,
            structural_units=tuple(structural_units),
            objects=tuple(object_values),
            candidate_text=candidate_text,
            text=candidate_text if document_certified else None,
            candidate_markdown=candidate_markdown,
            markdown=candidate_markdown if document_certified else None,
            status=(
                AssemblyStatus.COMPLETE
                if document_certified
                else AssemblyStatus.UNRESOLVED
            ),
            diagnostics=diagnostics,
        )

    @staticmethod
    def _validate_types(
        *,
        page: CropInput,
        geometry: GeometryResult,
        objects: ObjectReconstructionResult,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        ownership: np.ndarray | None,
        ownership_segment_ids: tuple[str, ...] | None,
    ) -> None:
        if not isinstance(page, CropInput):
            raise DocumentAssemblyInvariantError("page must be a CropInput")
        if not isinstance(geometry, GeometryResult):
            raise DocumentAssemblyInvariantError("geometry must be a GeometryResult")
        if not isinstance(objects, ObjectReconstructionResult):
            raise DocumentAssemblyInvariantError(
                "objects must be an ObjectReconstructionResult"
            )
        if not isinstance(plan, BlockPlan):
            raise DocumentAssemblyInvariantError("plan must be a BlockPlan")
        if type(crops) is not tuple or any(
            not isinstance(item, BlockCropPair) for item in crops
        ):
            raise DocumentAssemblyInvariantError(
                "crops must be an immutable BlockCropPair tuple"
            )
        if not isinstance(queue, OcrQueueResult):
            raise DocumentAssemblyInvariantError("queue must be an OcrQueueResult")
        if not isinstance(fusion, OcrFusionResult):
            raise DocumentAssemblyInvariantError("fusion must be an OcrFusionResult")
        if (ownership is None) != (ownership_segment_ids is None):
            raise DocumentAssemblyInvariantError(
                "ownership raster and segment order must be supplied together"
            )
        if ownership is not None and not isinstance(ownership, np.ndarray):
            raise DocumentAssemblyInvariantError(
                "ownership must be a NumPy array or None"
            )
        if ownership_segment_ids is not None and type(
            ownership_segment_ids
        ) is not tuple:
            raise DocumentAssemblyInvariantError(
                "ownership segment order must be an immutable tuple or None"
            )

    def _check_limits(
        self,
        *,
        page: CropInput,
        geometry: GeometryResult,
        objects: ObjectReconstructionResult,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
    ) -> None:
        counts = (
            ("segment", len(geometry.segmentation.segments), self.config.max_segments),
            ("object", len(objects.objects), self.config.max_objects),
            ("block", len(plan.blocks), self.config.max_blocks),
            ("job", len(queue.jobs), self.config.max_jobs),
        )
        for name, value, limit in counts:
            if value > limit:
                raise DocumentAssemblyLimitError(
                    f"{name} count {value} exceeds configured limit {limit}"
                )
        observed_characters = sum(
            len(item.output.text)
            for item in queue.jobs
            if item.output is not None
        )
        if observed_characters > self.config.max_text_characters:
            raise DocumentAssemblyLimitError(
                "observed OCR text exceeds configured character limit"
            )
        routed_word_characters = sum(
            len(word.text)
            for item in queue.jobs
            if item.output is not None
            for word in item.output.words
        )
        if routed_word_characters > self.config.max_text_characters:
            raise DocumentAssemblyLimitError(
                "observed OCR word text exceeds configured character limit"
            )
        crop_sizes = tuple(
            size
            for item in crops
            for size in (
                len(item.raw.png_bytes),
                len(item.gamma.png_bytes),
                (
                    len(item.isolation_mask_png)
                    if item.isolation_mask_png is not None
                    else 0
                ),
            )
            if size
        )
        if any(size > self.config.max_crop_bytes for size in crop_sizes):
            raise DocumentAssemblyLimitError(
                "one crop payload exceeds configured byte limit"
            )
        if sum(crop_sizes) > self.config.max_total_crop_bytes:
            raise DocumentAssemblyLimitError(
                "aggregate crop payload exceeds configured byte limit"
            )
        potential_slices = (
            len(fusion.observations)
            + len(fusion.group_observations)
            + len(fusion.block_text_observations)
        )
        if potential_slices > self.config.max_evidence_slices:
            raise DocumentAssemblyLimitError(
                "potential evidence slices exceed configured limit"
            )

    @staticmethod
    def _validate_provenance(
        *,
        page: CropInput,
        geometry: GeometryResult,
        objects: ObjectReconstructionResult,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        ownership: np.ndarray | None,
        ownership_segment_ids: tuple[str, ...] | None,
        object_config: ObjectReconstructionConfig | None,
        planning_config: BlockPlanningConfig | None,
        crop_config: BlockCropConfig | None,
        fusion_config: OcrFusionConfig | None,
    ) -> None:
        segmentation = geometry.segmentation
        try:
            expected_objects = ObjectReconstructor(object_config).reconstruct(
                aligned_size=segmentation.aligned_size,
                segments=segmentation.segments,
                rules=segmentation.rules,
                matrix=geometry.matrix,
            )
        except Exception as exc:
            raise DocumentAssemblyInvariantError(
                f"stage 6 object reconstruction failed validation: {exc}"
            ) from exc
        if objects != expected_objects:
            raise DocumentAssemblyInvariantError(
                "stage 6 object result was not reconstructed from geometry"
            )
        try:
            expected_plan = OverlappingBlockPlanner(planning_config).plan(
                aligned_size=segmentation.aligned_size,
                segments=segmentation.segments,
                objects_result=objects,
                matrix=geometry.matrix,
            )
        except Exception as exc:
            raise DocumentAssemblyInvariantError(
                f"stage 5 block planning failed validation: {exc}"
            ) from exc
        if plan != expected_plan:
            raise DocumentAssemblyInvariantError(
                "stage 5 block plan was not derived from geometry and objects"
            )
        owner_by_segment = {
            item.segment_id: item.object_id for item in objects.segment_ownership
        }
        for block, crop in zip(plan.blocks, crops):
            block_owner = owner_by_segment[block.segment_ids[0]]
            if any(
                segment_id not in owner_by_segment
                or owner_by_segment[segment_id] == block_owner
                for segment_id in crop.masked_segment_ids
            ):
                raise DocumentAssemblyInvariantError(
                    f"stage 5 crop {crop.block_id} has invalid foreign ownership"
                )
        try:
            expected_crops, aligned_rgb_sha256 = BlockCropper(
                crop_config
            ).crop_with_rgb_sha256(
                page,
                aligned_size=segmentation.aligned_size,
                plan=plan,
                ownership=ownership,
                ownership_segment_ids=ownership_segment_ids,
                isolation_source=crops,
            )
        except Exception as exc:
            raise DocumentAssemblyInvariantError(
                f"stage 4 crop generation failed validation: {exc}"
            ) from exc
        if crops != expected_crops:
            raise DocumentAssemblyInvariantError(
                "stage 4 crops were not derived from the supplied aligned page"
            )
        if geometry.aligned_rgb_sha256 != aligned_rgb_sha256:
            raise DocumentAssemblyInvariantError(
                "stage 1 geometry was not derived from the supplied aligned RGB page"
            )
        if (
            plan.aligned_size != objects.aligned_size
            or plan.source_segment_ids != objects.source_segment_ids
            or fusion.source_segment_ids != plan.source_segment_ids
        ):
            raise DocumentAssemblyInvariantError(
                "stage 1/6/5/2 segment order or canvas disagrees"
            )
        for block in plan.blocks:
            expected_core_owners = tuple(
                dict.fromkeys(owner_by_segment[item] for item in block.core_segment_ids)
            )
            if block.object_ids != expected_core_owners:
                raise DocumentAssemblyInvariantError(
                    "stage 5 block object IDs do not match core segment ownership"
                )
        try:
            expected_fusion = OcrEvidenceFusion(fusion_config).fuse(
                plan=plan,
                segments=segmentation.segments,
                crops=crops,
                queue=queue,
            )
        except Exception as exc:
            raise DocumentAssemblyInvariantError(
                f"stage 2 queue/crop/fusion validation failed: {exc}"
            ) from exc
        if fusion != expected_fusion:
            raise DocumentAssemblyInvariantError(
                "stage 2 fusion was not derived from the supplied queue and crops"
            )

    @staticmethod
    def _segment_assemblies(
        *,
        objects: ObjectReconstructionResult,
        fusion: OcrFusionResult,
        owner_by_segment: dict[str, str],
    ) -> tuple[SegmentTextAssembly, ...]:
        values = []
        for item in fusion.segments:
            candidate = item.selected_text or ""
            certified = (
                item.selected_text is not None
                and bool(compact_ocr_text(item.selected_text))
                and not item.unresolved
            )
            reasons = tuple(item.uncertainty_reasons)
            if not candidate:
                reasons = tuple(dict.fromkeys(reasons + ("no-segment-text",)))
            values.append(
                SegmentTextAssembly(
                    segment_id=item.segment_id,
                    object_id=owner_by_segment[item.segment_id],
                    candidate_text=candidate,
                    text=candidate if certified else None,
                    attribution_level=AttributionLevel.SEGMENT,
                    selected_observation_id=item.selected_observation_id,
                    evidence_slice_ids=(),
                    status=(
                        AssemblyStatus.COMPLETE
                        if certified
                        else AssemblyStatus.UNRESOLVED
                    ),
                    reasons=reasons,
                )
            )
        if tuple(item.segment_id for item in values) != objects.source_segment_ids:
            raise DocumentAssemblyInvariantError(
                "segment assemblies do not follow stage 6 source order"
            )
        return tuple(values)

    @staticmethod
    def _block_decisions(
        *,
        plan: BlockPlan,
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
    ) -> tuple[_BlockDecision, ...]:
        by_block: dict[str, list[BlockTextObservation]] = {
            item.block_id: [] for item in plan.blocks
        }
        for item in fusion.block_text_observations:
            by_block[item.block_id].append(item)
        jobs_by_block: dict[str, list[OcrJobResult]] = {
            item.block_id: [] for item in plan.blocks
        }
        for item in queue.jobs:
            jobs_by_block[item.block_id].append(item)
        values = []
        for block in plan.blocks:
            observations = tuple(by_block[block.block_id])
            scheduled_jobs = tuple(jobs_by_block[block.block_id])
            candidate = next(
                (
                    item
                    for item in observations
                    if item.transform is OcrTransform.RAW
                ),
                observations[0] if observations else None,
            )
            reasons: list[str] = []
            if not observations:
                values.append(_BlockDecision(block, None, None, None, (), ()))
                continue
            if any(item.source_replica_conflict for item in observations):
                reasons.append("capability-replica-conflict")
            if any(item.status is not OcrJobStatus.COMPLETE for item in scheduled_jobs):
                reasons.append("capability-job-failed")
            by_capability: dict[
                str, dict[OcrTransform, list[BlockTextObservation]]
            ] = {}
            for item in observations:
                by_capability.setdefault(item.capability_id, {}).setdefault(
                    item.transform, []
                ).append(item)
            stable: list[BlockTextObservation] = []
            # A WORD_BOXES capability is represented by segment observations,
            # not by block-text observations.  Require the RAW/GAMMA pair only
            # for capabilities that actually emitted TEXT_ONLY evidence.
            capability_ids = tuple(by_capability)
            for capability_id in capability_ids:
                transforms = by_capability.get(capability_id, {})
                raw = transforms.get(OcrTransform.RAW, [])
                gamma = transforms.get(OcrTransform.GAMMA, [])
                if not raw or not gamma:
                    reasons.append("missing-raw-gamma-text-pair")
                    continue
                all_values = tuple(raw + gamma)
                compact_values = {compact_ocr_text(item.text) for item in all_values}
                if len(compact_values) != 1:
                    reasons.append("raw-gamma-text-conflict")
                    continue
                stable.append(raw[0])
            if stable and len({compact_ocr_text(item.text) for item in stable}) != 1:
                reasons.append("capability-text-conflict")
            stable_candidate = stable[0] if stable else None
            certified = stable_candidate if stable_candidate is not None and not reasons else None
            corroborating = tuple(
                item
                for item in observations
                if stable_candidate is not None
                and item is not stable_candidate
                and compact_ocr_text(item.text)
                == compact_ocr_text(stable_candidate.text)
            )
            if stable_candidate is not None:
                candidate = stable_candidate
            values.append(
                _BlockDecision(
                    block=block,
                    candidate=candidate,
                    stable=stable_candidate,
                    certified=certified,
                    corroborating=corroborating,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )
        return tuple(values)

    @staticmethod
    def _membership_group_recoveries(
        *,
        fusion: OcrFusionResult,
        owner_by_segment: dict[str, str],
        observation_by_id: dict[str, SegmentGroupObservation],
    ) -> dict[str, tuple[_MembershipGroupRecovery, ...]]:
        values: dict[str, list[_MembershipGroupRecovery]] = {}
        observations_by_unit: dict[str, list[SegmentGroupObservation]] = {}
        for observation in fusion.group_observations:
            observations_by_unit.setdefault(
                observation.unit_id, []
            ).append(observation)
        for item in fusion.segment_groups:
            owners = {owner_by_segment[value] for value in item.segment_ids}
            if len(owners) != 1:
                raise DocumentAssemblyInvariantError(
                    "segment-group evidence crosses Stage 6 object owners"
                )
            object_id = next(iter(owners))
            selected = (
                observation_by_id.get(item.selected_observation_id)
                if item.selected_observation_id is not None
                else None
            )
            candidate = item.selected_text or ""
            corroborating = tuple(
                observation
                for observation in observations_by_unit.get(item.unit_id, ())
                if compact_ocr_text(observation.text)
                == compact_ocr_text(candidate)
                and compact_ocr_text(candidate)
            )
            placements = tuple(
                _Placement(
                    _EvidenceDraft(
                        job_id=observation.job_id,
                        observation_id=observation.observation_id,
                        block_id=observation.block_id,
                        transform=observation.transform,
                        lane_id=observation.lane_id,
                        capability_id=observation.capability_id,
                        input_sha256=observation.input_sha256,
                        context_sha256=observation.context_sha256,
                        object_id=object_id,
                        segment_ids=item.segment_ids,
                        attribution_level=AttributionLevel.SEGMENT_GROUP,
                        text=observation.text,
                    ),
                    0,
                    len(candidate),
                )
                for observation in corroborating
            )
            values.setdefault(object_id, []).append(
                _MembershipGroupRecovery(
                    object_id=object_id,
                    unit_id=item.unit_id,
                    segment_ids=item.segment_ids,
                    candidate_text=candidate,
                    placements=placements,
                    certified=(
                        selected is not None
                        and not item.unresolved
                        and bool(compact_ocr_text(candidate))
                    ),
                    reasons=item.uncertainty_reasons,
                )
            )
        return {
            object_id: tuple(items) for object_id, items in values.items()
        }

    def _recover_segment_groups(
        self,
        *,
        plan: BlockPlan,
        objects: ObjectReconstructionResult,
        block_decisions: tuple[_BlockDecision, ...],
        owner_by_segment: dict[str, str],
    ) -> tuple[dict[str, _GroupRecovery], frozenset[tuple[str, str]]]:
        """Certify one-sided XOR text only when the overlap is a unique anchor.

        The anchor block must be exactly the geometric intersection and both the
        intersection and residual must be whole Stage 6 objects.  This keeps the
        subtraction structural: a repeated anchor or a residual on both sides is
        deliberately left unresolved.
        """

        decision_by_id = {item.block.block_id: item for item in block_decisions}
        object_by_id = {item.object_id: item for item in objects.objects}
        proposals: dict[str, list[_GroupRecovery]] = {}
        search_characters = 0

        for algebra in plan.adjacent_algebra:
            first = decision_by_id[algebra.first_block_id]
            second = decision_by_id[algebra.second_block_id]
            directions = (
                (first, second, algebra.second_only_segment_ids),
                (second, first, algebra.first_only_segment_ids),
            )
            for anchor, source, residual_segment_ids in directions:
                if (
                    anchor.stable is None
                    or source.stable is None
                    or anchor.block.segment_ids
                    != algebra.intersection_segment_ids
                    or not residual_segment_ids
                ):
                    continue
                anchor_owners = {
                    owner_by_segment[item]
                    for item in algebra.intersection_segment_ids
                }
                residual_owners = {
                    owner_by_segment[item] for item in residual_segment_ids
                }
                if len(anchor_owners) != 1 or len(residual_owners) != 1:
                    continue
                anchor_object = object_by_id[next(iter(anchor_owners))]
                residual_object_id = next(iter(residual_owners))
                residual_object = object_by_id[residual_object_id]
                if (
                    anchor_object.segment_ids != algebra.intersection_segment_ids
                    or residual_object.segment_ids != residual_segment_ids
                    or anchor_object.kind is ObjectKind.TABLE
                    or residual_object.kind is ObjectKind.TABLE
                ):
                    continue

                anchor_text = anchor.stable.text
                supporting = (source.stable, *source.corroborating)
                residuals: list[tuple[BlockTextObservation, str]] = []
                for observation in supporting:
                    search_characters += len(anchor_text) + len(observation.text)
                    if search_characters > self.config.max_anchor_search_characters:
                        raise DocumentAssemblyLimitError(
                            "overlap anchor searches exceed configured character limit"
                        )
                    residual_value = self._unique_one_sided_residual(
                        source=observation.text,
                        anchor=anchor_text,
                    )
                    if residual_value is None:
                        residuals = []
                        break
                    residual, residual_precedes_anchor = residual_value
                    if residual_precedes_anchor != (
                        residual_object.reading_index
                        < anchor_object.reading_index
                    ):
                        residuals = []
                        break
                    residuals.append((observation, residual))
                if not residuals or len(
                    {compact_ocr_text(item[1]) for item in residuals}
                ) != 1:
                    continue

                candidate = residuals[0][1]
                drafts = tuple(
                    self._block_draft(
                        observation,
                        document_object=residual_object,
                        text=text,
                        segment_ids=residual_segment_ids,
                        attribution_level=AttributionLevel.SEGMENT_GROUP,
                    )
                    for observation, text in residuals
                )
                recovery = _GroupRecovery(
                    object_id=residual_object_id,
                    segment_ids=residual_segment_ids,
                    candidate_text=candidate,
                    placements=tuple(
                        _Placement(item, 0, len(candidate)) for item in drafts
                    ),
                    pair=(algebra.first_block_id, algebra.second_block_id),
                    certified=(
                        anchor.certified is not None
                        and source.certified is not None
                    ),
                    reasons=tuple(
                        dict.fromkeys(anchor.reasons + source.reasons)
                    ),
                )
                proposals.setdefault(residual_object_id, []).append(recovery)

        recovered: dict[str, _GroupRecovery] = {}
        recovered_pairs: set[tuple[str, str]] = set()
        for object_id, values in proposals.items():
            compact_values = {
                compact_ocr_text(item.candidate_text) for item in values
            }
            if len(compact_values) != 1:
                continue
            primary = values[0]
            combined = replace(
                primary,
                placements=tuple(
                    placement for item in values for placement in item.placements
                ),
            )
            recovered[object_id] = combined
            recovered_pairs.update(
                item.pair for item in values if item.certified
            )
        return recovered, frozenset(recovered_pairs)

    @staticmethod
    def _unique_one_sided_residual(
        *,
        source: str,
        anchor: str,
    ) -> tuple[str, bool] | None:
        compact_source = compact_ocr_text(source)
        compact_anchor = compact_ocr_text(anchor)
        if not compact_anchor or len(compact_anchor) >= len(compact_source):
            return None
        start = compact_source.find(compact_anchor)
        if start < 0 or compact_source.find(compact_anchor, start + 1) >= 0:
            return None
        anchor_stop = start + len(compact_anchor)
        has_prefix = start > 0
        has_suffix = anchor_stop < len(compact_source)
        if has_prefix == has_suffix:
            return None
        non_whitespace = tuple(
            index for index, character in enumerate(source) if not character.isspace()
        )
        if has_prefix:
            anchor_raw_start = non_whitespace[start]
            raw_start = non_whitespace[0]
            raw_stop = non_whitespace[start - 1] + 1
            boundary = source[raw_stop:anchor_raw_start]
        else:
            anchor_raw_stop = non_whitespace[anchor_stop - 1] + 1
            raw_start = non_whitespace[anchor_stop]
            raw_stop = non_whitespace[-1] + 1
            boundary = source[anchor_raw_stop:raw_start]
        if not boundary or not any(character.isspace() for character in boundary):
            return None
        residual = source[raw_start:raw_stop]
        return (
            (residual, has_prefix)
            if compact_ocr_text(residual)
            else None
        )

    def _render_object(
        self,
        *,
        document_object: DocumentObject,
        span_by_id: dict[str, SegmentSpan],
        segment_assembly_by_id: dict[str, SegmentTextAssembly],
        fused_by_id: dict[str, SegmentFusion],
        observation_by_id: dict[str, SegmentObservation],
        job_by_id: dict[str, OcrJobResult],
        block_decisions: tuple[_BlockDecision, ...],
        group_recovery: _GroupRecovery | None,
        membership_groups: tuple[_MembershipGroupRecovery, ...],
        structural_drafts: tuple[_StructuralDraft, ...],
    ) -> _RenderedObject:
        if membership_groups:
            return self._render_membership_groups(
                document_object=document_object,
                groups=membership_groups,
                structural_drafts=structural_drafts,
                span_by_id=span_by_id,
                segment_assembly_by_id=segment_assembly_by_id,
                fused_by_id=fused_by_id,
                observation_by_id=observation_by_id,
                job_by_id=job_by_id,
            )
        direct = tuple(
            item
            for item in block_decisions
            if item.candidate is not None
            and item.block.segment_ids == document_object.segment_ids
        )
        if direct and document_object.kind is not ObjectKind.TABLE:
            compact_values = {
                compact_ocr_text((item.stable or item.candidate).text)
                for item in direct
                if item.stable is not None or item.candidate is not None
            }
            if group_recovery is not None:
                compact_values.add(compact_ocr_text(group_recovery.candidate_text))
            if len(compact_values) == 1:
                decision = direct[0]
                primary = decision.stable or decision.candidate
                assert primary is not None
                drafts = tuple(
                    self._block_draft(
                        item,
                        document_object=document_object,
                    )
                    for item in (primary, *decision.corroborating)
                    if compact_ocr_text(item.text) == compact_ocr_text(primary.text)
                )
                placements = tuple(
                    _Placement(item, 0, len(primary.text)) for item in drafts
                )
                segment_candidate, _ = self._render_segment_values(
                    document_object=document_object,
                    segment_values=tuple(
                        segment_assembly_by_id[item]
                        for item in document_object.segment_ids
                    ),
                    span_by_id=span_by_id,
                    fused_by_id=fused_by_id,
                    observation_by_id=observation_by_id,
                    job_by_id=job_by_id,
                )
                complete_segment_evidence = all(
                    segment_assembly_by_id[item].status
                    is AssemblyStatus.COMPLETE
                    for item in document_object.segment_ids
                )
                if (
                    (
                        complete_segment_evidence
                        and compact_ocr_text(segment_candidate)
                        != compact_ocr_text(primary.text)
                    )
                    or self._explicit_segment_conflict(
                        candidate=primary.text,
                        segment_values=tuple(
                            segment_assembly_by_id[item]
                            for item in document_object.segment_ids
                        ),
                    )
                ):
                    return _RenderedObject(
                        candidate_text=primary.text,
                        certified=False,
                        attribution_level=AttributionLevel.OBJECT,
                        placements=placements,
                        reasons=("object-text-segment-conflict",),
                    )
                decision_reasons = tuple(
                    dict.fromkeys(
                        reason for item in direct for reason in item.reasons
                    )
                )
                direct_certified = all(
                    item.certified is not None for item in direct
                )
                return _RenderedObject(
                    candidate_text=primary.text,
                    certified=direct_certified,
                    attribution_level=AttributionLevel.OBJECT,
                    placements=placements,
                    reasons=(
                        ()
                        if direct_certified
                        else decision_reasons
                        or ("object-text-not-fully-corroborated",)
                    ),
                )
            primary = direct[0].stable or direct[0].candidate
            assert primary is not None
            draft = self._block_draft(primary, document_object=document_object)
            return _RenderedObject(
                candidate_text=primary.text,
                certified=False,
                attribution_level=AttributionLevel.OBJECT,
                placements=(_Placement(draft, 0, len(primary.text)),),
                reasons=("object-text-source-conflict",),
            )

        if group_recovery is not None:
            segment_candidate, _ = self._render_segment_values(
                document_object=document_object,
                segment_values=tuple(
                    segment_assembly_by_id[item]
                    for item in document_object.segment_ids
                ),
                span_by_id=span_by_id,
                fused_by_id=fused_by_id,
                observation_by_id=observation_by_id,
                job_by_id=job_by_id,
            )
            complete_segment_evidence = all(
                segment_assembly_by_id[item].status is AssemblyStatus.COMPLETE
                for item in document_object.segment_ids
            )
            if (
                (
                    complete_segment_evidence
                    and compact_ocr_text(segment_candidate)
                    != compact_ocr_text(group_recovery.candidate_text)
                )
                or self._explicit_segment_conflict(
                    candidate=group_recovery.candidate_text,
                    segment_values=tuple(
                        segment_assembly_by_id[item]
                        for item in document_object.segment_ids
                    ),
                )
            ):
                return _RenderedObject(
                    candidate_text=group_recovery.candidate_text,
                    certified=False,
                    attribution_level=AttributionLevel.SEGMENT_GROUP,
                    placements=group_recovery.placements,
                    reasons=("segment-group-text-conflict",),
                )
            return _RenderedObject(
                candidate_text=group_recovery.candidate_text,
                certified=group_recovery.certified,
                attribution_level=AttributionLevel.SEGMENT_GROUP,
                placements=group_recovery.placements,
                reasons=(
                    ()
                    if group_recovery.certified
                    else group_recovery.reasons
                    or ("segment-group-not-fully-corroborated",)
                ),
            )

        segment_values = tuple(
            segment_assembly_by_id[item] for item in document_object.segment_ids
        )
        candidate_available = any(item.candidate_text for item in segment_values)
        certified = all(item.status is AssemblyStatus.COMPLETE for item in segment_values)
        candidate, placements = self._render_segment_values(
            document_object=document_object,
            segment_values=segment_values,
            span_by_id=span_by_id,
            fused_by_id=fused_by_id,
            observation_by_id=observation_by_id,
            job_by_id=job_by_id,
        )
        reasons = tuple(
            dict.fromkeys(
                reason
                for item in segment_values
                for reason in item.reasons
            )
        )
        if not candidate_available:
            reasons = tuple(dict.fromkeys(reasons + ("no-attributable-object-text",)))
        return _RenderedObject(
            candidate_text=candidate,
            certified=certified and bool(compact_ocr_text(candidate)),
            attribution_level=AttributionLevel.SEGMENT,
            placements=placements,
            reasons=reasons,
        )

    def _render_membership_groups(
        self,
        *,
        document_object: DocumentObject,
        groups: tuple[_MembershipGroupRecovery, ...],
        structural_drafts: tuple[_StructuralDraft, ...],
        span_by_id: dict[str, SegmentSpan],
        segment_assembly_by_id: dict[str, SegmentTextAssembly],
        fused_by_id: dict[str, SegmentFusion],
        observation_by_id: dict[str, SegmentObservation],
        job_by_id: dict[str, OcrJobResult],
    ) -> _RenderedObject:
        object_order = {
            segment_id: index
            for index, segment_id in enumerate(document_object.segment_ids)
        }
        draft_members = {item.segment_ids for item in structural_drafts}
        group_by_segment: dict[str, _MembershipGroupRecovery] = {}
        group_first: dict[int, _MembershipGroupRecovery] = {}
        reasons: list[str] = []
        certified = True
        for group in groups:
            if any(item not in object_order for item in group.segment_ids):
                raise DocumentAssemblyInvariantError(
                    "membership group crosses its Stage 6 object"
                )
            indexes = tuple(object_order[item] for item in group.segment_ids)
            if indexes != tuple(range(min(indexes), max(indexes) + 1)):
                reasons.append("segment-group-noncontiguous")
                certified = False
            whole_flow_object = (
                document_object.kind is not ObjectKind.TABLE
                and group.segment_ids == document_object.segment_ids
            )
            if group.segment_ids not in draft_members and not whole_flow_object:
                reasons.append("segment-group-grammar-unsplittable")
                certified = False
            for segment_id in group.segment_ids:
                if segment_id in group_by_segment:
                    raise DocumentAssemblyInvariantError(
                        "membership groups overlap inside one object"
                    )
                group_by_segment[segment_id] = group
            group_first[min(indexes)] = group
            reasons.extend(group.reasons)
            certified = certified and group.certified

        pieces: list[str] = []
        placements: list[_Placement] = []
        cursor = 0
        previous_row: int | None = None
        previous_column: int | None = None

        def append_value(
            text: str,
            *,
            row: int,
            column: int,
            local_placements: tuple[_Placement, ...],
        ) -> None:
            nonlocal cursor, previous_row, previous_column
            if not text:
                return
            if pieces:
                separator = (
                    "\n"
                    if previous_row is not None and row != previous_row
                    else "\t"
                    if document_object.kind is ObjectKind.TABLE
                    and previous_column is not None
                    and column != previous_column
                    else " "
                )
                pieces.append(separator)
                cursor += len(separator)
            start = cursor
            pieces.append(text)
            cursor += len(text)
            placements.extend(
                _Placement(
                    draft=item.draft,
                    start=start + item.start,
                    stop=start + item.stop,
                )
                for item in local_placements
            )
            previous_row = row
            previous_column = column

        for index, segment_id in enumerate(document_object.segment_ids):
            group = group_by_segment.get(segment_id)
            if group is not None:
                if group_first.get(index) is not group:
                    continue
                span = span_by_id[group.segment_ids[0]]
                append_value(
                    group.candidate_text,
                    row=span.row_start,
                    column=span.column_start,
                    local_placements=group.placements,
                )
                continue
            segment_value = segment_assembly_by_id[segment_id]
            segment_text, segment_placements = self._render_segment_values(
                document_object=document_object,
                segment_values=(segment_value,),
                span_by_id=span_by_id,
                fused_by_id=fused_by_id,
                observation_by_id=observation_by_id,
                job_by_id=job_by_id,
            )
            span = span_by_id[segment_id]
            append_value(
                segment_text,
                row=span.row_start,
                column=span.column_start,
                local_placements=segment_placements,
            )
            certified = certified and (
                segment_value.status is AssemblyStatus.COMPLETE
            )
            reasons.extend(segment_value.reasons)

        candidate = "".join(pieces)
        if not compact_ocr_text(candidate):
            reasons.append("no-attributable-object-text")
            certified = False
        return _RenderedObject(
            candidate_text=candidate,
            certified=certified,
            attribution_level=AttributionLevel.SEGMENT_GROUP,
            placements=tuple(placements),
            reasons=tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _explicit_segment_conflict(
        *,
        candidate: str,
        segment_values: tuple[SegmentTextAssembly, ...],
    ) -> bool:
        """Return true when certified segment evidence cannot occur in order."""

        compact_candidate = compact_ocr_text(candidate)
        cursor = 0
        for item in segment_values:
            if not item.candidate_text:
                continue
            compact_segment = compact_ocr_text(item.candidate_text)
            position = compact_candidate.find(compact_segment, cursor)
            if position < 0:
                return True
            cursor = position + len(compact_segment)
        return False

    def _structural_drafts(
        self,
        *,
        document_object: DocumentObject,
        span_by_id: dict[str, SegmentSpan],
        matrix: SparseSegmentMatrix,
        coordinates_by_segment: dict[str, tuple[tuple[int, int], ...]],
    ) -> tuple[_StructuralDraft, ...]:
        spans = tuple(span_by_id[item] for item in document_object.segment_ids)
        if document_object.kind is ObjectKind.TABLE:
            object_segment_ids = set(document_object.segment_ids)
            local_cell_map: dict[tuple[int, int], list[str]] = {}
            for segment_id in document_object.segment_ids:
                for coordinate in coordinates_by_segment[segment_id]:
                    local_cell_map.setdefault(coordinate, []).append(segment_id)
            covered_coordinates = set(local_cell_map)
            for segment_id in document_object.segment_ids:
                span = span_by_id[segment_id]
                span_area = (span.row_stop - span.row_start) * (
                    span.column_stop - span.column_start
                )
                if len(coordinates_by_segment[segment_id]) != span_area:
                    raise DocumentAssemblyInvariantError(
                        "a table cell has non-rectangular sparse occupancy"
                    )
            parent = {item: item for item in document_object.segment_ids}

            def find(segment_id: str) -> str:
                root = segment_id
                while parent[root] != root:
                    root = parent[root]
                while parent[segment_id] != segment_id:
                    following = parent[segment_id]
                    parent[segment_id] = root
                    segment_id = following
                return root

            source_order = {
                segment_id: index
                for index, segment_id in enumerate(document_object.segment_ids)
            }
            for segment_ids in local_cell_map.values():
                members = tuple(
                    item for item in segment_ids if item in object_segment_ids
                )
                if len(members) < 2:
                    continue
                first_root = find(members[0])
                for member in members[1:]:
                    second_root = find(member)
                    if first_root == second_root:
                        continue
                    if source_order[first_root] <= source_order[second_root]:
                        parent[second_root] = first_root
                    else:
                        parent[first_root] = second_root
                        first_root = second_root
            grouped: dict[str, list[str]] = {}
            for segment_id in document_object.segment_ids:
                grouped.setdefault(find(segment_id), []).append(segment_id)

            values: list[_StructuralDraft] = []
            for segment_ids in grouped.values():
                group_spans = tuple(span_by_id[item] for item in segment_ids)
                group_row_start = min(item.row_start for item in group_spans)
                group_row_stop = max(item.row_stop for item in group_spans)
                group_column_start = min(item.column_start for item in group_spans)
                group_column_stop = max(item.column_stop for item in group_spans)
                group_coordinates = {
                    coordinate
                    for segment_id in segment_ids
                    for coordinate in coordinates_by_segment[segment_id]
                }
                group_area = (group_row_stop - group_row_start) * (
                    group_column_stop - group_column_start
                )
                if len(group_coordinates) != group_area:
                    raise DocumentAssemblyInvariantError(
                        "overlapping table fragments do not form one rectangle"
                    )
                values.append(
                    _StructuralDraft(
                        unit_kind=StructuralUnitKind.TABLE_CELL,
                        segment_ids=tuple(segment_ids),
                        row_start=group_row_start,
                        row_stop=group_row_stop,
                        column_start=group_column_start,
                        column_stop=group_column_stop,
                    )
                )
            row_start = document_object.row_start
            row_stop = document_object.row_stop
            column_start = document_object.column_start
            column_stop = document_object.column_stop
            rule_rows = set(matrix.horizontal_rule_rows)
            rule_columns = set(matrix.vertical_rule_columns)
            logical_rows = sum(
                row not in rule_rows for row in range(row_start, row_stop)
            )
            logical_columns = sum(
                column not in rule_columns
                for column in range(column_start, column_stop)
            )
            if logical_rows * logical_columns > self.config.max_structural_units:
                raise DocumentAssemblyLimitError(
                    "table structural grid exceeds configured unit limit"
                )
            values.extend(
                _StructuralDraft(
                    unit_kind=StructuralUnitKind.TABLE_CELL,
                    segment_ids=(),
                    row_start=row,
                    row_stop=row + 1,
                    column_start=column,
                    column_stop=column + 1,
                )
                for row in range(row_start, row_stop)
                for column in range(column_start, column_stop)
                if row not in rule_rows
                and column not in rule_columns
                if (row, column) not in covered_coordinates
            )
            return tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.row_start,
                        item.column_start,
                        item.row_stop,
                        item.column_stop,
                        item.segment_ids,
                    ),
                )
            )

        unit_kind = {
            ObjectKind.PARAGRAPH: StructuralUnitKind.PARAGRAPH_LINE,
            ObjectKind.LIST: StructuralUnitKind.LIST_ITEM,
        }.get(document_object.kind, StructuralUnitKind.UNKNOWN_FRAGMENT)
        grouped_rows: dict[int, list[str]] = {}
        for segment_id in document_object.segment_ids:
            grouped_rows.setdefault(span_by_id[segment_id].row_start, []).append(
                segment_id
            )
        return tuple(
            _StructuralDraft(
                unit_kind=unit_kind,
                segment_ids=tuple(segment_ids),
                row_start=min(span_by_id[item].row_start for item in segment_ids),
                row_stop=max(span_by_id[item].row_stop for item in segment_ids),
                column_start=min(
                    span_by_id[item].column_start for item in segment_ids
                ),
                column_stop=max(
                    span_by_id[item].column_stop for item in segment_ids
                ),
            )
            for _, segment_ids in sorted(grouped_rows.items())
        )

    @staticmethod
    def _table_axes(
        *,
        document_object: DocumentObject,
        matrix: SparseSegmentMatrix,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if document_object.kind is not ObjectKind.TABLE:
            return (), ()
        row_start = document_object.row_start
        row_stop = document_object.row_stop
        column_start = document_object.column_start
        column_stop = document_object.column_stop
        rule_rows = set(matrix.horizontal_rule_rows)
        rule_columns = set(matrix.vertical_rule_columns)
        return (
            tuple(
                row
                for row in range(row_start, row_stop)
                if row not in rule_rows
            ),
            tuple(
                column
                for column in range(column_start, column_stop)
                if column not in rule_columns
            ),
        )

    @staticmethod
    def _block_draft(
        item: BlockTextObservation,
        *,
        document_object: DocumentObject,
        text: str | None = None,
        segment_ids: tuple[str, ...] | None = None,
        attribution_level: AttributionLevel = AttributionLevel.OBJECT,
    ) -> _EvidenceDraft:
        return _EvidenceDraft(
            job_id=item.job_id,
            observation_id=None,
            block_id=item.block_id,
            transform=item.transform,
            lane_id=item.lane_id,
            capability_id=item.capability_id,
            input_sha256=item.input_sha256,
            context_sha256=item.context_sha256,
            object_id=document_object.object_id,
            segment_ids=segment_ids or document_object.segment_ids,
            attribution_level=attribution_level,
            text=item.text if text is None else text,
        )

    def _render_segment_values(
        self,
        *,
        document_object: DocumentObject,
        segment_values: tuple[SegmentTextAssembly, ...],
        span_by_id: dict[str, SegmentSpan],
        fused_by_id: dict[str, SegmentFusion],
        observation_by_id: dict[str, SegmentObservation],
        job_by_id: dict[str, OcrJobResult],
    ) -> tuple[str, tuple[_Placement, ...]]:
        pieces: list[str] = []
        placements: list[_Placement] = []
        cursor = 0
        previous_row: int | None = None
        previous_column: int | None = None
        for item in segment_values:
            if not item.candidate_text:
                continue
            span = span_by_id[item.segment_id]
            if pieces:
                separator = (
                    "\n"
                    if previous_row is not None and span.row_start != previous_row
                    else "\t"
                    if document_object.kind is ObjectKind.TABLE
                    and previous_column is not None
                    and span.column_start != previous_column
                    else " "
                )
                pieces.append(separator)
                cursor += len(separator)
            start = cursor
            if start + len(item.candidate_text) > self.config.max_text_characters:
                raise DocumentAssemblyLimitError(
                    "one assembled object exceeds configured character limit"
                )
            pieces.append(item.candidate_text)
            stop = start + len(item.candidate_text)
            cursor = stop
            fused = fused_by_id[item.segment_id]
            if fused.selected_observation_id is not None:
                observation = observation_by_id[fused.selected_observation_id]
                job = job_by_id[observation.job_id]
                draft = _EvidenceDraft(
                    job_id=observation.job_id,
                    observation_id=observation.observation_id,
                    block_id=observation.block_id,
                    transform=observation.transform,
                    lane_id=observation.lane_id,
                    capability_id=observation.capability_id,
                    input_sha256=observation.input_sha256,
                    context_sha256=observation.context_sha256,
                    object_id=document_object.object_id,
                    segment_ids=(item.segment_id,),
                    attribution_level=AttributionLevel.SEGMENT,
                    text=observation.text,
                )
                if job.output is None:
                    raise DocumentAssemblyInvariantError(
                        "selected observation references a failed OCR job"
                    )
                placements.append(_Placement(draft, start, stop))
            previous_row = span.row_start
            previous_column = span.column_start
        return "".join(pieces), tuple(placements)

    @staticmethod
    def _global_reasons(
        *,
        geometry: GeometryResult,
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        block_decisions: tuple[_BlockDecision, ...],
        recovered_overlap_pairs: frozenset[tuple[str, str]],
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        resolved_group_segments = {
            segment_id
            for group in fusion.segment_groups
            if not group.unresolved
            for segment_id in group.segment_ids
        }
        if geometry.status is not GeometryStatus.COMPLETE:
            reasons.append("degraded-geometry")
        if queue.status is not OcrQueueStatus.COMPLETE:
            reasons.append("ocr-queue-partial")
        if fusion.unassigned_word_observations:
            reasons.append("unassigned-word-evidence")
        if fusion.replica_conflicts:
            reasons.append("capability-replica-conflict")
        if any(
            item.conflicting_intersection_segment_ids
            for item in fusion.overlaps
        ):
            reasons.append("overlap-text-conflict")
        if any(
            item.missing_intersection_segment_ids
            and not set(item.missing_intersection_segment_ids).issubset(
                resolved_group_segments
            )
            and (item.first_block_id, item.second_block_id)
            not in recovered_overlap_pairs
            for item in fusion.overlaps
        ):
            reasons.append("overlap-evidence-missing")
        if any(item.unresolved for item in fusion.segment_groups):
            reasons.append("segment-group-unresolved")
        if any(item.candidate is not None and item.certified is None for item in block_decisions):
            reasons.append("unresolved-block-text")
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _join_with_offsets(
        values: tuple[str, ...],
        *,
        separator: str,
    ) -> tuple[str, tuple[tuple[int, int], ...]]:
        pieces: list[str] = []
        offsets: list[tuple[int, int]] = []
        cursor = 0
        for index, value in enumerate(values):
            if index:
                pieces.append(separator)
                cursor += len(separator)
            start = cursor
            pieces.append(value)
            cursor += len(value)
            offsets.append((start, cursor))
        return "".join(pieces), tuple(offsets)

    @staticmethod
    def _finalize_evidence(
        *,
        rendered: tuple[_RenderedObject, ...],
        object_offsets: tuple[tuple[int, int], ...],
        candidate_text: str,
    ) -> tuple[tuple[EvidenceSlice, ...], tuple[tuple[str, ...], ...]]:
        evidence: list[EvidenceSlice] = []
        by_object: list[tuple[str, ...]] = []
        compact_output_cache: dict[tuple[int, int], str] = {}
        compact_evidence_cache: dict[str, str] = {}
        for object_index, item in enumerate(rendered):
            object_start = object_offsets[object_index][0] if object_offsets else 0
            identifiers: list[str] = []
            for placement in item.placements:
                start = object_start + placement.start
                stop = object_start + placement.stop
                output_key = (start, stop)
                compact_output = compact_output_cache.get(output_key)
                if compact_output is None and 0 <= start < stop <= len(
                    candidate_text
                ):
                    compact_output = compact_ocr_text(
                        candidate_text[start:stop]
                    )
                    compact_output_cache[output_key] = compact_output
                compact_evidence = compact_evidence_cache.get(
                    placement.draft.text
                )
                if compact_evidence is None:
                    compact_evidence = compact_ocr_text(
                        placement.draft.text
                    )
                    compact_evidence_cache[
                        placement.draft.text
                    ] = compact_evidence
                if (
                    start < 0
                    or stop > len(candidate_text)
                    or stop <= start
                    or compact_output != compact_evidence
                ):
                    raise DocumentAssemblyInvariantError(
                        "assembled evidence offsets do not match observed text"
                    )
                slice_id = f"evidence-{len(evidence):08d}"
                identifiers.append(slice_id)
                draft = placement.draft
                evidence.append(
                    EvidenceSlice(
                        slice_id=slice_id,
                        job_id=draft.job_id,
                        observation_id=draft.observation_id,
                        block_id=draft.block_id,
                        transform=draft.transform,
                        lane_id=draft.lane_id,
                        capability_id=draft.capability_id,
                        input_sha256=draft.input_sha256,
                        context_sha256=draft.context_sha256,
                        object_id=draft.object_id,
                        segment_ids=draft.segment_ids,
                        attribution_level=draft.attribution_level,
                        text=draft.text,
                        output_start=start,
                        output_stop=stop,
                    )
                )
            by_object.append(tuple(identifiers))
        return tuple(evidence), tuple(by_object)

    @staticmethod
    def _fallback_evidence_slices(
        *,
        decisions: tuple[_BlockDecision, ...],
        candidate_text: str,
        first_index: int,
    ) -> tuple[EvidenceSlice, ...]:
        offsets: dict[str, tuple[int, int]] = {}
        cursor = 0
        for text in dict.fromkeys(
            item.candidate.text
            for item in decisions
            if item.candidate is not None
        ):
            if offsets:
                cursor += 1
            offsets[text] = (cursor, cursor + len(text))
            cursor += len(text)
        if cursor != len(candidate_text):
            raise DocumentAssemblyInvariantError(
                "fallback evidence offsets disagree with candidate output"
            )

        values: list[EvidenceSlice] = []
        for decision in decisions:
            primary = decision.candidate
            if primary is None:
                continue
            start, stop = offsets[primary.text]
            observations = (primary, *decision.corroborating)
            for observation in observations:
                if compact_ocr_text(observation.text) != compact_ocr_text(
                    primary.text
                ):
                    continue
                values.append(
                    EvidenceSlice(
                        slice_id=f"evidence-{first_index + len(values):08d}",
                        job_id=observation.job_id,
                        observation_id=None,
                        block_id=observation.block_id,
                        transform=observation.transform,
                        lane_id=observation.lane_id,
                        capability_id=observation.capability_id,
                        input_sha256=observation.input_sha256,
                        context_sha256=observation.context_sha256,
                        object_id=None,
                        segment_ids=decision.block.segment_ids,
                        attribution_level=AttributionLevel.UNATTRIBUTABLE,
                        text=observation.text,
                        output_start=start,
                        output_stop=stop,
                    )
                )
        return tuple(values)

    @staticmethod
    def _object_markdown(
        kind: ObjectKind,
        text: str,
        units: tuple[StructuralUnit, ...],
        *,
        table_row_indices: tuple[int, ...] = (),
        table_column_indices: tuple[int, ...] = (),
    ) -> str:
        if kind is not ObjectKind.TABLE or not units:
            return _markdown_for_object(kind, text)
        logical_rows = table_row_indices
        logical_columns = table_column_indices
        if not logical_rows or not logical_columns:
            raise DocumentAssemblyInvariantError(
                "table Markdown requires complete logical axes"
            )
        cells: dict[tuple[int, int], str] = {}
        covered: set[tuple[int, int]] = set()
        for unit in units:
            anchor = (unit.row_start, unit.column_start)
            unit_coordinates = {
                (row, column)
                for row in logical_rows
                if unit.row_start <= row < unit.row_stop
                for column in logical_columns
                if unit.column_start <= column < unit.column_stop
            }
            if anchor not in unit_coordinates:
                raise DocumentAssemblyInvariantError(
                    "table unit anchor lies outside its logical grid"
                )
            if covered.intersection(unit_coordinates):
                raise DocumentAssemblyInvariantError(
                    "table structural units overlap ambiguously"
                )
            covered.update(unit_coordinates)
            escaped = (
                unit.candidate_text.replace("\\", "\\\\")
                .replace("|", "\\|")
                .replace("\r", " ")
                .replace("\n", " ")
            )
            if anchor in cells:
                raise DocumentAssemblyInvariantError(
                    "table structural units share one anchor"
                )
            cells[anchor] = escaped
            for row in logical_rows:
                if not unit.row_start <= row < unit.row_stop:
                    continue
                for column in logical_columns:
                    if not unit.column_start <= column < unit.column_stop:
                        continue
                    coordinate = (row, column)
                    if coordinate == anchor:
                        continue
                    if row == unit.row_start:
                        marker = "::merge-left::"
                    elif column == unit.column_start:
                        marker = "::merge-up::"
                    else:
                        marker = "::merge-up-left::"
                    existing = cells.setdefault(coordinate, marker)
                    if existing != marker:
                        raise DocumentAssemblyInvariantError(
                            "table structural units overlap ambiguously"
                        )
        expected_coordinates = {
            (row, column)
            for row in logical_rows
            for column in logical_columns
        }
        if covered != expected_coordinates:
            raise DocumentAssemblyInvariantError(
                "table structural units do not exactly cover the logical grid"
            )
        return "\n".join(
            "| "
            + " | ".join(
                cells.get((row, column), "")
                for column in logical_columns
            )
            + " |"
            for row in logical_rows
        )


__all__ = [
    "AssemblyStatus",
    "AttributionLevel",
    "DocumentAssembler",
    "DocumentAssemblyConfig",
    "DocumentAssemblyInvariantError",
    "DocumentAssemblyLimitError",
    "DocumentAssemblyResult",
    "EvidenceSlice",
    "ObjectTextAssembly",
    "SegmentTextAssembly",
    "StructuralUnit",
    "StructuralUnitKind",
]
