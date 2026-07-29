from app.pipeline_core.contracts import (
    CandidateDecision,
    PageSegmentArtifact,
    PdfTextLayerArtifact,
    PipelineCapabilities,
    PipelineRecipe,
    PipelineStageTrace,
    RecognizedSegment,
    RecognitionBatch,
    StructuralRenderArtifact,
    SpanCandidate,
    SpanEvidence,
    recipe_for,
)
from app.pipeline_core.candidates import select_observed_span_candidate
from app.pipeline_core.orchestrator import recognize_segments

__all__ = (
    "PageSegmentArtifact",
    "CandidateDecision",
    "PdfTextLayerArtifact",
    "PipelineCapabilities",
    "PipelineRecipe",
    "PipelineStageTrace",
    "RecognizedSegment",
    "RecognitionBatch",
    "StructuralRenderArtifact",
    "SpanCandidate",
    "SpanEvidence",
    "recipe_for",
    "recognize_segments",
    "select_observed_span_candidate",
)
