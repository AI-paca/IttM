from __future__ import annotations

from dataclasses import dataclass

from app.pipeline_core import SpanCandidate, SpanEvidence, select_observed_span_candidate
from app.recognition.languages import language_script, script_counts
from app.recognition.quality import bbox_overlap_ratio


@dataclass(frozen=True)
class TextTokenDecision:
    primary: str
    selected: str
    primary_source: str
    selected_source: str
    bbox: tuple[int, int, int, int]
    selected_confidence: float


def _script_consistency(source: str, text: str) -> int:
    counts = script_counts(text)
    alpha = sum(counts.get(name, 0) for name in ("latin", "cyrillic", "cjk", "greek"))
    if alpha == 0:
        return 1000
    return round(1000 * counts.get(language_script(source), 0) / alpha)


def _same_word_box(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    first_width = max(1, first[2] - first[0])
    second_width = max(1, second[2] - second[0])
    first_height = max(1, first[3] - first[1])
    second_height = max(1, second[3] - second[1])
    return (
        bbox_overlap_ratio(first, second) >= 0.85
        and min(first_width, second_width) / max(first_width, second_width) >= 0.75
        and min(first_height, second_height) / max(first_height, second_height) >= 0.75
    )


def repair_text_from_aligned_word_candidates(
    primary_text: str,
    candidate_passes: tuple[tuple[str, list[dict]], ...],
    *,
    minimum_selected_confidence: float = 80.0,
    minimum_confidence_gain: float = 20.0,
) -> tuple[str, tuple[TextTokenDecision, ...]]:
    observations = [
        (source, word)
        for source, words in candidate_passes
        for word in words
        if str(word.get("text", "")).strip() and (bbox := word.get("bbox")) and len(bbox) == 4
    ]
    clusters: list[list[tuple[str, dict]]] = []
    for source, word in observations:
        matching = [
            cluster
            for cluster in clusters
            if any(
                _same_word_box(tuple(word["bbox"]), tuple(existing["bbox"])) for _existing_source, existing in cluster
            )
        ]
        if not matching:
            clusters.append([(source, word)])
        elif len(matching) == 1:
            matching[0].append((source, word))

    result = primary_text
    decisions = []
    for cluster_index, cluster in enumerate(clusters):
        primary_observations = [(source, word) for source, word in cluster if str(word["text"]).strip() in result]
        if not primary_observations:
            continue
        current_source, current_word = max(
            primary_observations,
            key=lambda item: float(item[1].get("conf", 0)),
        )
        current_text = str(current_word["text"]).strip()
        exact_support: dict[str, int] = {}
        for _source, word in cluster:
            key = str(word["text"]).strip().casefold()
            exact_support[key] = exact_support.get(key, 0) + 1
        candidates = []
        for candidate_id, (source, word) in enumerate(cluster):
            text = str(word["text"]).strip()
            confidence = max(0.0, min(100.0, float(word.get("conf", 0))))
            candidates.append(
                SpanCandidate(
                    candidate_id=candidate_id,
                    span_id=f"text-token-{cluster_index}",
                    text=text,
                    source=source,
                    bbox=tuple(int(value) for value in word["bbox"]),
                    evidence=SpanEvidence(
                        ocr_confidence_milli=round(confidence * 10),
                        script_consistency_milli=_script_consistency(source, text),
                        context_consistency_milli=1000,
                        source_agreement=exact_support[text.casefold()],
                    ),
                )
            )
        try:
            selected, _decision = select_observed_span_candidate(tuple(candidates))
        except RuntimeError:
            continue
        selected_confidence = next(
            float(word.get("conf", 0))
            for source, word in cluster
            if source == selected.source and str(word.get("text", "")).strip() == selected.text
        )
        current_confidence = float(current_word.get("conf", 0))
        if (
            selected.text == current_text
            or selected_confidence < minimum_selected_confidence
            or selected_confidence - current_confidence < minimum_confidence_gain
            or result.count(current_text) != 1
        ):
            continue
        result = result.replace(current_text, selected.text, 1)
        decisions.append(
            TextTokenDecision(
                primary=current_text,
                selected=selected.text,
                primary_source=current_source,
                selected_source=selected.source,
                bbox=selected.bbox,
                selected_confidence=selected_confidence,
            )
        )
    return result, tuple(decisions)
