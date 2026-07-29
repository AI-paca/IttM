from __future__ import annotations

from dataclasses import dataclass

Box = tuple[int, int, int, int]
SparseCode = tuple[int, int, int]
Counter = tuple[str, int]


@dataclass(frozen=True)
class PipelineCapabilities:
    """What a recognition adapter already guarantees.

    Recipes are derived from capabilities so trusted API output cannot
    accidentally pass through Tesseract-specific repair stages.
    """

    trusted_text: bool = False
    provides_layout: bool = False
    provides_markdown: bool = False
    needs_language_retry: bool = True


@dataclass(frozen=True)
class PipelineRecipe:
    stages: tuple[str, ...]


def recipe_for(capabilities: PipelineCapabilities) -> PipelineRecipe:
    from app.pipeline_core.native import native_pipeline_core

    native = native_pipeline_core()
    if native is not None:
        bits = (
            int(capabilities.trusted_text)
            | (int(capabilities.provides_layout) << 1)
            | (int(capabilities.provides_markdown) << 2)
            | (int(capabilities.needs_language_retry) << 3)
        )
        mask = native.recipe_mask(bits)
        ordered_stages = (
            "align",
            "segment",
            "project_sparse",
            "recognize_segments",
            "select_language_candidate",
            "lexical_correction",
            "group_structures",
            "render_markdown",
        )
        return PipelineRecipe(
            stages=tuple(
                stage
                for index, stage in enumerate(ordered_stages)
                if mask & (1 << index)
            )
        )

    stages: list[str] = []
    if not capabilities.provides_layout:
        stages.extend(("align", "segment", "project_sparse"))
    stages.append("recognize_segments")
    if capabilities.needs_language_retry and not capabilities.trusted_text:
        stages.append("select_language_candidate")
    if not capabilities.trusted_text:
        stages.append("lexical_correction")
    if not capabilities.provides_markdown:
        stages.extend(("group_structures", "render_markdown"))
    return PipelineRecipe(stages=tuple(stages))


@dataclass(frozen=True)
class PipelineStageTrace:
    """Serializable observation emitted at one deterministic stage boundary."""

    version: int
    stage: str
    input_count: int
    output_count: int
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpanEvidence:
    """Language-neutral evidence attached to one observed OCR span."""

    ocr_confidence_milli: int
    script_consistency_milli: int
    context_consistency_milli: int
    source_agreement: int
    contradictions: int = 0


@dataclass(frozen=True)
class SpanCandidate:
    """A candidate whose emitted text and bbox came from an OCR adapter."""

    candidate_id: int
    span_id: str
    text: str
    source: str
    bbox: Box
    evidence: SpanEvidence


@dataclass(frozen=True)
class CandidateDecision:
    span_id: str
    selected_candidate_id: int
    scores: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class RecognizedSegment:
    """Image-free output of the recognition stage.

    Platform adapters may use PIL, Canvas, native Tesseract, or Tesseract.js,
    but no platform image or OCR-engine object can cross this boundary.
    """

    kind: str
    bbox: Box
    parts: tuple[str, ...]
    anchor: tuple[int, int] | None = None
    codes: tuple[SparseCode, ...] = ()
    list_marker: bool = False
    content_left: int | None = None
    counters: tuple[Counter, ...] = ()
    flags: tuple[str, ...] = ()

    def counter(self, name: str) -> int:
        return dict(self.counters).get(name, 0)


@dataclass(frozen=True)
class RecognitionBatch:
    segments: tuple[RecognizedSegment, ...]
    totals: tuple[Counter, ...]
    flags: tuple[str, ...]
    trace: PipelineStageTrace

    def total(self, name: str) -> int:
        return dict(self.totals).get(name, 0)


@dataclass(frozen=True)
class StructuralRenderArtifact:
    parts: tuple[str, ...]
    flags: tuple[str, ...]
    bypassed: bool
    lossy_merge_rejected: bool
    lint_pass: bool | None
    confirmed: bool


@dataclass(frozen=True)
class PageSegmentArtifact:
    """Typed page-stage result; flags remain diagnostic, never control flow."""

    markdown: str
    counters: tuple[Counter, ...]
    flags: tuple[str, ...]
    structural_lint_pass: bool | None
    structural_confirmed: bool

    def counter(self, name: str) -> int:
        return dict(self.counters).get(name, 0)

    def metadata(self) -> dict[str, object]:
        return {**dict(self.counters), "runtime_flags": list(self.flags)}

    def __iter__(self):
        """Keep the legacy tuple boundary while callers migrate atomically."""

        yield self.markdown
        yield self.metadata()

    @classmethod
    def from_legacy_tuple(
        cls,
        value: tuple[str, dict],
    ) -> "PageSegmentArtifact":
        """Temporary compatibility for tests/plugins returning the pre-v16 shape."""

        markdown, metadata = value
        flags = tuple(metadata.get("runtime_flags", ()))
        return cls(
            markdown=markdown,
            counters=tuple(
                (name, int(metadata.get(name, 0)))
                for name in ("chunks", "cards_found", "tables_found", "table_cells")
            ),
            flags=flags,
            structural_lint_pass=(True if "markdown_lint:pass" in flags else None),
            structural_confirmed="structural_grammar:finite_merge_v1" in flags,
        )


@dataclass(frozen=True)
class PdfTextLayerArtifact:
    pages: tuple[str | None, ...]
    counters: tuple[Counter, ...]
    flags: tuple[str, ...]
    layout_step: str

    def metadata(self) -> dict[str, object]:
        return {**dict(self.counters), "runtime_flags": list(self.flags)}

    def __iter__(self):
        yield list(self.pages)
        yield self.metadata()
