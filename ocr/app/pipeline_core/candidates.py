from __future__ import annotations

from app.pipeline_core.contracts import CandidateDecision, SpanCandidate
from app.pipeline_core.native import native_pipeline_core


def select_observed_span_candidate(
    candidates: tuple[SpanCandidate, ...],
) -> tuple[SpanCandidate, CandidateDecision]:
    """Select an observed candidate using the shared Rust evidence scorer."""

    if not candidates:
        raise ValueError("At least one observed span candidate is required")
    span_ids = {candidate.span_id for candidate in candidates}
    if len(span_ids) != 1:
        raise ValueError("All candidates must describe the same span")
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Candidate ids must be unique within a span")
    for candidate in candidates:
        if not candidate.text or not candidate.source:
            raise ValueError("Candidates require observed text and source")
        left, top, right, bottom = candidate.bbox
        if right <= left or bottom <= top:
            raise ValueError("Candidates require a non-empty source bbox")

    core = native_pipeline_core()
    if core is None:
        raise RuntimeError("Candidate scoring requires the shared pipeline core")

    scored = []
    for candidate in candidates:
        evidence = candidate.evidence
        score = core.span_evidence_score(
            evidence.ocr_confidence_milli,
            evidence.script_consistency_milli,
            evidence.context_consistency_milli,
            evidence.source_agreement,
            evidence.contradictions,
        )
        scored.append((candidate.candidate_id, score))
    selected_id, _score = min(scored, key=lambda item: (-item[1], item[0]))
    selected = next(candidate for candidate in candidates if candidate.candidate_id == selected_id)
    return selected, CandidateDecision(
        span_id=selected.span_id,
        selected_candidate_id=selected_id,
        scores=tuple(sorted(scored)),
    )
