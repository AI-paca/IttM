"""Clean-room recursive sparse document pipeline.

Public exports are resolved lazily so a geometry-free semantic stage does not
silently import Pillow, NumPy, OCR engines, or the legacy conversion service.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AssemblyStatus": (
        "app.sparse_pipeline.document_assembly",
        "AssemblyStatus",
    ),
    "AttributionLevel": (
        "app.sparse_pipeline.document_assembly",
        "AttributionLevel",
    ),
    "BoundedGrammar": ("app.sparse_pipeline.bounded_grammar", "BoundedGrammar"),
    "BlockCropConfig": ("app.sparse_pipeline.block_crops", "BlockCropConfig"),
    "BlockCropInvariantError": (
        "app.sparse_pipeline.block_crops",
        "BlockCropInvariantError",
    ),
    "BlockCropLimitError": (
        "app.sparse_pipeline.block_crops",
        "BlockCropLimitError",
    ),
    "BlockCropPair": ("app.sparse_pipeline.block_crops", "BlockCropPair"),
    "BlockCropper": ("app.sparse_pipeline.block_crops", "BlockCropper"),
    "BlockArtifactWriter": (
        "app.sparse_pipeline.block_artifacts",
        "BlockArtifactWriter",
    ),
    "BlockPlan": ("app.sparse_pipeline.block_planning", "BlockPlan"),
    "BlockPlanStatus": (
        "app.sparse_pipeline.block_planning",
        "BlockPlanStatus",
    ),
    "BlockPlanningConfig": (
        "app.sparse_pipeline.block_planning",
        "BlockPlanningConfig",
    ),
    "BlockPlanningInvariantError": (
        "app.sparse_pipeline.block_planning",
        "BlockPlanningInvariantError",
    ),
    "BlockPlanningMode": (
        "app.sparse_pipeline.block_planning",
        "BlockPlanningMode",
    ),
    "BlockPlanningLimitError": (
        "app.sparse_pipeline.block_planning",
        "BlockPlanningLimitError",
    ),
    "BlockSetAlgebra": (
        "app.sparse_pipeline.block_planning",
        "BlockSetAlgebra",
    ),
    "MembershipUnit": (
        "app.sparse_pipeline.block_planning",
        "MembershipUnit",
    ),
    "MembershipUnitKind": (
        "app.sparse_pipeline.block_planning",
        "MembershipUnitKind",
    ),
    "CANDIDATE_ROLE": (
        "app.sparse_pipeline.crop_enhancement",
        "CANDIDATE_ROLE",
    ),
    "GrammarDecision": ("app.sparse_pipeline.bounded_grammar", "GrammarDecision"),
    "Box": ("app.sparse_pipeline.contracts", "Box"),
    "CropEnhancementArtifactWriter": (
        "app.sparse_pipeline.enhancement_artifacts",
        "CropEnhancementArtifactWriter",
    ),
    "CropEnhancementBackendError": (
        "app.sparse_pipeline.crop_enhancement",
        "CropEnhancementBackendError",
    ),
    "CropEnhancementConfig": (
        "app.sparse_pipeline.crop_enhancement",
        "CropEnhancementConfig",
    ),
    "CropEnhancementInvariantError": (
        "app.sparse_pipeline.crop_enhancement",
        "CropEnhancementInvariantError",
    ),
    "CropEnhancementLimitError": (
        "app.sparse_pipeline.crop_enhancement",
        "CropEnhancementLimitError",
    ),
    "CropInput": ("app.sparse_pipeline.crop_enhancement", "CropInput"),
    "EnhancedCrop": ("app.sparse_pipeline.crop_enhancement", "EnhancedCrop"),
    "EnhancementBackend": (
        "app.sparse_pipeline.crop_enhancement",
        "EnhancementBackend",
    ),
    "EnhancementStatus": (
        "app.sparse_pipeline.crop_enhancement",
        "EnhancementStatus",
    ),
    "GammaDarkCropEnhancer": (
        "app.sparse_pipeline.crop_enhancement",
        "GammaDarkCropEnhancer",
    ),
    "GeometryResult": ("app.sparse_pipeline.contracts", "GeometryResult"),
    "SparseCoordinateMode": (
        "app.sparse_pipeline.contracts",
        "SparseCoordinateMode",
    ),
    "SparseStructuralCode": (
        "app.sparse_pipeline.contracts",
        "SparseStructuralCode",
    ),
    "Segment": ("app.sparse_pipeline.contracts", "Segment"),
    "SegmentKind": ("app.sparse_pipeline.contracts", "SegmentKind"),
    "ControlArtifactWriter": (
        "app.sparse_pipeline.control_artifacts",
        "ControlArtifactWriter",
    ),
    "GeometryAnalyzer": ("app.sparse_pipeline.geometry", "GeometryAnalyzer"),
    "GeometryBundle": ("app.sparse_pipeline.geometry", "GeometryBundle"),
    "V16GeometryAnalyzer": (
        "app.sparse_pipeline.v16_geometry",
        "V16GeometryAnalyzer",
    ),
    "V16GeometryBundle": (
        "app.sparse_pipeline.v16_geometry",
        "V16GeometryBundle",
    ),
    "RecursiveGridConfig": (
        "app.sparse_pipeline.v16_recursive_grid",
        "RecursiveGridConfig",
    ),
    "RecursiveStopFlag": (
        "app.sparse_pipeline.v16_recursive_grid",
        "RecursiveStopFlag",
    ),
    "GeometryConfig": ("app.sparse_pipeline.geometry", "GeometryConfig"),
    "GeometryArtifactWriter": (
        "app.sparse_pipeline.geometry_artifacts",
        "GeometryArtifactWriter",
    ),
    "ObjectArtifactWriter": (
        "app.sparse_pipeline.object_artifacts",
        "ObjectArtifactWriter",
    ),
    "DocumentObject": (
        "app.sparse_pipeline.object_reconstruction",
        "DocumentObject",
    ),
    "DocumentAssembler": (
        "app.sparse_pipeline.document_assembly",
        "DocumentAssembler",
    ),
    "DocumentArtifactWriter": (
        "app.sparse_pipeline.document_artifacts",
        "DocumentArtifactWriter",
    ),
    "DocumentAssemblyConfig": (
        "app.sparse_pipeline.document_assembly",
        "DocumentAssemblyConfig",
    ),
    "DocumentAssemblyInvariantError": (
        "app.sparse_pipeline.document_assembly",
        "DocumentAssemblyInvariantError",
    ),
    "DocumentAssemblyLimitError": (
        "app.sparse_pipeline.document_assembly",
        "DocumentAssemblyLimitError",
    ),
    "DocumentAssemblyResult": (
        "app.sparse_pipeline.document_assembly",
        "DocumentAssemblyResult",
    ),
    "EvidenceSlice": (
        "app.sparse_pipeline.document_assembly",
        "EvidenceSlice",
    ),
    "ObjectKind": ("app.sparse_pipeline.object_reconstruction", "ObjectKind"),
    "ObjectTextAssembly": (
        "app.sparse_pipeline.document_assembly",
        "ObjectTextAssembly",
    ),
    "ObjectReconstructionConfig": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructionConfig",
    ),
    "ObjectReconstructionInvariantError": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructionInvariantError",
    ),
    "ObjectReconstructionLimitError": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructionLimitError",
    ),
    "ObjectReconstructionResult": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructionResult",
    ),
    "ObjectReconstructionStatus": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructionStatus",
    ),
    "ObjectReconstructor": (
        "app.sparse_pipeline.object_reconstruction",
        "ObjectReconstructor",
    ),
    "OverlappingBlockPlanner": (
        "app.sparse_pipeline.block_planning",
        "OverlappingBlockPlanner",
    ),
    "RecognitionBlock": (
        "app.sparse_pipeline.block_planning",
        "RecognitionBlock",
    ),
    "EasyOcrConfig": ("app.sparse_pipeline.ocr_adapters", "EasyOcrConfig"),
    "GlmOcrConfig": ("app.sparse_pipeline.ocr_adapters", "GlmOcrConfig"),
    "OcrArtifactWriter": (
        "app.sparse_pipeline.ocr_artifacts",
        "OcrArtifactWriter",
    ),
    "OcrEvidenceFusion": (
        "app.sparse_pipeline.ocr_fusion",
        "OcrEvidenceFusion",
    ),
    "OcrFusionConfig": (
        "app.sparse_pipeline.ocr_fusion",
        "OcrFusionConfig",
    ),
    "OcrFusionResult": (
        "app.sparse_pipeline.ocr_fusion",
        "OcrFusionResult",
    ),
    "OcrRoutingMode": (
        "app.sparse_pipeline.ocr_fusion",
        "OcrRoutingMode",
    ),
    "SegmentGroupFusion": (
        "app.sparse_pipeline.ocr_fusion",
        "SegmentGroupFusion",
    ),
    "SegmentGroupObservation": (
        "app.sparse_pipeline.ocr_fusion",
        "SegmentGroupObservation",
    ),
    "OcrLane": ("app.sparse_pipeline.ocr_queue", "OcrLane"),
    "OcrQueueConfig": ("app.sparse_pipeline.ocr_queue", "OcrQueueConfig"),
    "OcrQueueResult": ("app.sparse_pipeline.ocr_queue", "OcrQueueResult"),
    "ParallelOcrQueue": (
        "app.sparse_pipeline.ocr_queue",
        "ParallelOcrQueue",
    ),
    "PersistentOcrSession": (
        "app.sparse_pipeline.ocr_session",
        "PersistentOcrSession",
    ),
    "TesseractConfig": (
        "app.sparse_pipeline.ocr_adapters",
        "TesseractConfig",
    ),
    "make_easyocr_lane": (
        "app.sparse_pipeline.ocr_adapters",
        "make_easyocr_lane",
    ),
    "make_glm_ocr_lane": (
        "app.sparse_pipeline.ocr_adapters",
        "make_glm_ocr_lane",
    ),
    "make_tesseract_lane": (
        "app.sparse_pipeline.ocr_adapters",
        "make_tesseract_lane",
    ),
    "SegmentObjectOwnership": (
        "app.sparse_pipeline.object_reconstruction",
        "SegmentObjectOwnership",
    ),
    "SegmentTextAssembly": (
        "app.sparse_pipeline.document_assembly",
        "SegmentTextAssembly",
    ),
    "SparsePipelineEvidence": (
        "app.sparse_pipeline.pipeline_evidence",
        "SparsePipelineEvidence",
    ),
    "SparsePipelineEvidenceInvariantError": (
        "app.sparse_pipeline.pipeline_evidence",
        "SparsePipelineEvidenceInvariantError",
    ),
    "sparse_matrix_sha256": (
        "app.sparse_pipeline.block_planning",
        "sparse_matrix_sha256",
    ),
    "sparse_matrix_payload": (
        "app.sparse_pipeline.block_planning",
        "sparse_matrix_payload",
    ),
    "SparsePageResult": (
        "app.sparse_pipeline.runtime",
        "SparsePageResult",
    ),
    "SparsePipelineRuntime": (
        "app.sparse_pipeline.runtime",
        "SparsePipelineRuntime",
    ),
    "SparseRuntimeConfig": (
        "app.sparse_pipeline.runtime",
        "SparseRuntimeConfig",
    ),
    "resolve_sparse_runtime_profile": (
        "app.sparse_pipeline.runtime",
        "resolve_sparse_runtime_profile",
    ),
    "StructuralUnit": (
        "app.sparse_pipeline.document_assembly",
        "StructuralUnit",
    ),
    "StructuralUnitKind": (
        "app.sparse_pipeline.document_assembly",
        "StructuralUnitKind",
    ),
    "TutorialArtifactWriter": (
        "app.sparse_pipeline.tutorial_artifacts",
        "TutorialArtifactWriter",
    ),
    "Derivation": ("app.sparse_pipeline.recursive_control", "Derivation"),
    "EvidenceAtom": ("app.sparse_pipeline.recursive_control", "EvidenceAtom"),
    "EvidenceView": ("app.sparse_pipeline.recursive_control", "EvidenceView"),
    "Expand": ("app.sparse_pipeline.recursive_control", "Expand"),
    "FailureMode": ("app.sparse_pipeline.recursive_control", "FailureMode"),
    "GrammarBudget": ("app.sparse_pipeline.recursive_control", "GrammarBudget"),
    "GrammarContext": ("app.sparse_pipeline.recursive_control", "GrammarContext"),
    "PipelineCancelled": (
        "app.sparse_pipeline.recursive_control",
        "PipelineCancelled",
    ),
    "PipelineInvariantError": (
        "app.sparse_pipeline.recursive_control",
        "PipelineInvariantError",
    ),
    "PipelineLimitError": (
        "app.sparse_pipeline.recursive_control",
        "PipelineLimitError",
    ),
    "RecursiveWhileReducer": (
        "app.sparse_pipeline.recursive_control",
        "RecursiveWhileReducer",
    ),
    "ReductionProposal": (
        "app.sparse_pipeline.recursive_control",
        "ReductionProposal",
    ),
    "RunLimits": ("app.sparse_pipeline.recursive_control", "RunLimits"),
    "RunOutcome": ("app.sparse_pipeline.recursive_control", "RunOutcome"),
    "RunStatus": ("app.sparse_pipeline.recursive_control", "RunStatus"),
    "SplitContext": ("app.sparse_pipeline.recursive_control", "SplitContext"),
    "Terminal": ("app.sparse_pipeline.recursive_control", "Terminal"),
    "WorkNode": ("app.sparse_pipeline.recursive_control", "WorkNode"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
