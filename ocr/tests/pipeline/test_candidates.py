from dataclasses import replace

import pytest

from app.pipeline_core import SpanCandidate, SpanEvidence, select_observed_span_candidate


def _candidate(candidate_id: int, text: str, confidence: int = 800) -> SpanCandidate:
    return SpanCandidate(
        candidate_id=candidate_id,
        span_id="row-2/phrase-1",
        text=text,
        source=f"ocr-pass-{candidate_id}",
        bbox=(10, 20, 80, 40),
        evidence=SpanEvidence(
            ocr_confidence_milli=confidence,
            script_consistency_milli=900,
            context_consistency_milli=700,
            source_agreement=2,
        ),
    )


def test_selects_only_an_observed_candidate_and_records_scores():
    primary = _candidate(10, "АЕРНА", 600)
    observed = _candidate(20, "ALPHA", 900)

    selected, decision = select_observed_span_candidate((primary, observed))

    assert selected is observed
    assert selected.text in {primary.text, observed.text}
    assert decision.selected_candidate_id == observed.candidate_id
    assert dict(decision.scores)[20] > dict(decision.scores)[10]


def test_selection_is_order_independent_and_ties_use_candidate_id():
    first = _candidate(3, "ALPHA")
    second = _candidate(2, "ALPHA")

    selected_forward, _ = select_observed_span_candidate((first, second))
    selected_reverse, _ = select_observed_span_candidate((second, first))

    assert selected_forward.candidate_id == 2
    assert selected_reverse.candidate_id == 2


@pytest.mark.parametrize(
    "candidates, message",
    [
        ((), "At least one"),
        (
            (_candidate(1, "one"), replace(_candidate(2, "two"), span_id="other")),
            "same span",
        ),
        ((_candidate(1, "one"), _candidate(1, "two")), "unique"),
        ((replace(_candidate(1, "one"), text=""),), "observed text"),
        ((replace(_candidate(1, "one"), bbox=(0, 0, 0, 1)),), "non-empty"),
    ],
)
def test_rejects_candidates_without_traceable_evidence(candidates, message):
    with pytest.raises(ValueError, match=message):
        select_observed_span_candidate(candidates)
