from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from app.chunking.vertical import TableLayout
from app.layout.table_slots import table_horizontal_spans
from app.pipeline_core import SpanCandidate, SpanEvidence, select_observed_span_candidate
from app.recognition.languages import SCRIPT_PATTERNS, language_script, script_counts

_NOISE = frozenset("#@$}{[]\\|")


@dataclass(frozen=True)
class ObservedPhrase:
    text: str
    bbox: tuple[int, int, int, int]
    confidence_milli: int


@dataclass(frozen=True)
class SpanFusionDecision:
    row: int
    start_col: int
    end_col: int
    selected_sources: tuple[str, ...]
    selected_text: str


@dataclass(frozen=True)
class CjkSpanCropDecision:
    row: int
    selected: str
    minimum_glyph_confidence: float
    crop_bbox: tuple[int, int, int, int]


@dataclass(frozen=True)
class RepeatedSpanIdentifierDecision:
    row: int
    primary: str
    selected: str
    repeated_label: str
    crop_confidence: float
    crop_bbox: tuple[int, int, int, int]


def words_outside_fused_spans(
    words: tuple[dict, ...] | list[dict],
    table: TableLayout,
    decisions: tuple[SpanFusionDecision, ...],
) -> list[dict]:
    span_rows = {decision.row for decision in decisions}
    if not span_rows:
        return list(words)
    result = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            result.append(word)
            continue
        y = (bbox[1] + bbox[3]) / 2
        if any(table.y_lines[row] < y < table.y_lines[row + 1] for row in span_rows):
            continue
        result.append(word)
    return result


def _union_bbox(
    boxes: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _split_word(
    text: str,
    bbox: tuple[int, int, int, int],
) -> list[tuple[str, tuple[int, int, int, int], bool]]:
    parts = text.split("/")
    if len(parts) == 1:
        return [(text, bbox, False)]
    width = max(1, bbox[2] - bbox[0])
    result = []
    cursor = 0
    for index, part in enumerate(parts):
        start = bbox[0] + round(width * cursor / max(1, len(text)))
        cursor += len(part)
        end = bbox[0] + round(width * cursor / max(1, len(text)))
        if part.strip():
            result.append((part, (start, bbox[1], max(start + 1, end), bbox[3]), False))
        if index < len(parts) - 1:
            cursor += 1
            result.append(("", (end, bbox[1], min(bbox[2], end + 1), bbox[3]), True))
    return result


def observed_slash_phrases(words: list[dict]) -> tuple[ObservedPhrase, ...]:
    phrases = []
    texts: list[str] = []
    boxes: list[tuple[int, int, int, int]] = []
    confidences: list[float] = []

    def flush() -> None:
        text = " ".join(texts).strip()
        if text and boxes:
            average = sum(confidences) / max(1, len(confidences))
            phrases.append(
                ObservedPhrase(
                    text=text,
                    bbox=_union_bbox(boxes),
                    confidence_milli=max(0, min(1000, round(average * 10))),
                )
            )
        texts.clear()
        boxes.clear()
        confidences.clear()

    ordered = sorted(words, key=lambda word: (word["bbox"][0], word["bbox"][1]))
    for word in ordered:
        raw_text = str(word.get("text", "")).strip()
        raw_bbox = word.get("bbox")
        if not raw_text or not raw_bbox or len(raw_bbox) != 4:
            continue
        bbox = tuple(int(value) for value in raw_bbox)
        for text, piece_bbox, separator in _split_word(raw_text, bbox):
            if separator:
                flush()
                continue
            cleaned = " ".join(text.split())
            if cleaned:
                texts.append(cleaned)
                boxes.append(piece_bbox)
                confidences.append(float(word.get("conf", 0)))
    flush()
    return tuple(phrases)


def _words_in_span(
    words: list[dict],
    bbox: tuple[int, int, int, int],
) -> list[dict]:
    left, top, right, bottom = bbox
    result = []
    for word in words:
        word_bbox = word.get("bbox")
        if not word_bbox or len(word_bbox) != 4:
            continue
        x = (word_bbox[0] + word_bbox[2]) / 2
        y = (word_bbox[1] + word_bbox[3]) / 2
        if left <= x <= right and top <= y <= bottom:
            result.append(word)
    return result


def _script_consistency_milli(source: str, text: str) -> int:
    counts = script_counts(text)
    alpha = sum(counts.get(name, 0) for name in ("latin", "cyrillic", "cjk", "greek"))
    if alpha == 0:
        return 0
    scripts = {
        language_script(language)
        for language in source.split("+")
        if language_script(language) in SCRIPT_PATTERNS
    }
    matching = max((counts.get(script, 0) for script in scripts), default=max(counts.values()))
    return round(1000 * matching / alpha)


def _context_consistency_milli(text: str, phrase_count: int, modal_count: int) -> int:
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0
    acceptable = sum(
        character.isalnum() or character in "-_.()," for character in visible
    )
    shape = 1000 if phrase_count == modal_count else 400
    return round((acceptable / len(visible)) * shape)


def _bbox_alignment_milli(
    candidate: tuple[int, int, int, int],
    anchor: tuple[int, int, int, int],
) -> int:
    intersection = max(0, min(candidate[2], anchor[2]) - max(candidate[0], anchor[0]))
    widest = max(1, max(candidate[2] - candidate[0], anchor[2] - anchor[0]))
    return round(1000 * intersection / widest)


def fuse_horizontal_span_candidates(
    image: Image.Image,
    table: TableLayout,
    primary_words: list[dict],
    candidate_passes: tuple[tuple[str, list[dict]], ...],
) -> tuple[list[dict], tuple[SpanFusionDecision, ...]]:
    replacements = []
    decisions = []
    for row, start_col, end_col in table_horizontal_spans(image, table):
        if end_col - start_col < 2:
            continue
        span_bbox = (
            table.x_lines[start_col],
            table.y_lines[row],
            table.x_lines[end_col + 1],
            table.y_lines[row + 1],
        )
        observed = []
        all_passes = (("primary", primary_words), *candidate_passes)
        for source, words in all_passes:
            phrases = observed_slash_phrases(_words_in_span(words, span_bbox))
            if len(phrases) >= 3:
                observed.append((source, phrases))
        if len(observed) < 2:
            continue
        anchor_source, anchor_phrases = max(
            observed,
            key=lambda item: (len(item[1]), item[0] == "primary"),
        )
        modal_count = len(anchor_phrases)
        aligned_by_phrase: list[list[tuple[str, ObservedPhrase, int]]] = [
            [(anchor_source, phrase, 1000)] for phrase in anchor_phrases
        ]
        for source, phrases in observed:
            if source == anchor_source and phrases == anchor_phrases:
                continue
            for phrase in phrases:
                matches = [
                    (_bbox_alignment_milli(phrase.bbox, anchor.bbox), index)
                    for index, anchor in enumerate(anchor_phrases)
                ]
                alignment, index = max(matches)
                if alignment >= 450:
                    aligned_by_phrase[index].append((source, phrase, alignment))

        selected_phrases = []
        selected_sources = []
        for phrase_index in range(modal_count):
            observations = aligned_by_phrase[phrase_index]
            values = [phrase.text.casefold() for _source, phrase, _alignment in observations]
            candidates = []
            for candidate_id, (source, phrase, alignment) in enumerate(observations):
                support = sum(value == phrase.text.casefold() for value in values)
                contradictions = sum(character in _NOISE for character in phrase.text)
                candidates.append(
                    SpanCandidate(
                        candidate_id=candidate_id,
                        span_id=f"table-row-{row}/phrase-{phrase_index}",
                        text=phrase.text,
                        source=source,
                        bbox=phrase.bbox,
                        evidence=SpanEvidence(
                            ocr_confidence_milli=phrase.confidence_milli,
                            script_consistency_milli=_script_consistency_milli(source, phrase.text),
                            context_consistency_milli=_context_consistency_milli(
                                phrase.text,
                                len(
                                    next(
                                        phrases
                                        for observed_source, phrases in observed
                                        if observed_source == source
                                    )
                                ),
                                modal_count,
                            )
                            * alignment
                            // 1000,
                            source_agreement=support,
                            contradictions=min(4, contradictions),
                        ),
                    )
                )
            try:
                selected, _decision = select_observed_span_candidate(tuple(candidates))
            except RuntimeError:
                selected = candidates[0]
            selected_phrases.append(selected.text)
            selected_sources.append(selected.source)

        selected_text = " / ".join(selected_phrases)
        primary_text = " / ".join(
            phrase.text
            for source, phrases in observed
            if source == "primary"
            for phrase in phrases
        )
        if not selected_text or selected_text == primary_text:
            continue
        replacements.append(
            {
                "text": selected_text,
                "bbox": span_bbox,
                "conf": 100,
                "span_sources": tuple(selected_sources),
            }
        )
        decisions.append(
            SpanFusionDecision(
                row=row,
                start_col=start_col,
                end_col=end_col,
                selected_sources=tuple(selected_sources),
                selected_text=selected_text,
            )
        )

    if not replacements:
        return list(primary_words), ()
    replacement_rows = {decision.row for decision in decisions}
    kept = []
    for word in primary_words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            kept.append(word)
            continue
        y = (bbox[1] + bbox[3]) / 2
        row = next(
            (
                index
                for index in replacement_rows
                if table.y_lines[index] < y < table.y_lines[index + 1]
            ),
            None,
        )
        if row is None:
            kept.append(word)
    return [*kept, *replacements], tuple(decisions)


def apply_slash_bounded_cjk_crops(
    engine,
    image: Image.Image,
    table: TableLayout,
    raw_words: list[dict],
    fused_words: list[dict],
    span_decisions: tuple[SpanFusionDecision, ...],
    *,
    minimum_glyph_confidence: float = 60.0,
) -> tuple[list[dict], tuple[CjkSpanCropDecision, ...], int]:
    recognize_cjk = getattr(engine, "recognize_cjk_phrase", None)
    if not callable(recognize_cjk) or not span_decisions:
        return list(fused_words), (), 0
    replacements: dict[int, dict] = {}
    decisions = []
    calls = 0
    for span in span_decisions:
        row_words = sorted(
            _words_in_span(
                raw_words,
                (
                    table.x_lines[span.start_col],
                    table.y_lines[span.row],
                    table.x_lines[span.end_col + 1],
                    table.y_lines[span.row + 1],
                ),
            ),
            key=lambda word: word["bbox"][0],
        )
        separators = [
            word
            for word in row_words
            if "/" in str(word.get("text", ""))
        ]
        if len(separators) != 4:
            continue
        crop_bbox = (
            separators[1]["bbox"][2] + 3,
            max(0, table.y_lines[span.row] + 3),
            separators[2]["bbox"][0] - 3,
            min(image.height, table.y_lines[span.row + 1] - 3),
        )
        if crop_bbox[2] <= crop_bbox[0] or crop_bbox[3] <= crop_bbox[1]:
            continue
        crop = image.crop(crop_bbox)
        try:
            text, confidence = recognize_cjk(crop)
        finally:
            crop.close()
        calls += 1
        if (
            confidence < minimum_glyph_confidence
            or not 2 <= script_counts(text).get("cjk", 0) <= 8
            or script_counts(text).get("cjk", 0) != len(text)
        ):
            continue
        parts = [part.strip() for part in span.selected_text.split("/")]
        if len(parts) != 5 or parts[2] == text:
            continue
        selected_text = " / ".join((*parts[:2], text, *parts[3:]))
        replacement = next(
            (
                word
                for word in fused_words
                if word.get("text") == span.selected_text
                and (bbox := word.get("bbox"))
                and len(bbox) == 4
                and table.y_lines[span.row]
                <= (bbox[1] + bbox[3]) / 2
                <= table.y_lines[span.row + 1]
            ),
            None,
        )
        if replacement is None:
            continue
        replacements[id(replacement)] = {
            **replacement,
            "text": selected_text,
            "cjk_crop_bbox": crop_bbox,
        }
        decisions.append(
            CjkSpanCropDecision(
                row=span.row,
                selected=text,
                minimum_glyph_confidence=confidence,
                crop_bbox=crop_bbox,
            )
        )
    if not replacements:
        return list(fused_words), (), calls
    return (
        [replacements.get(id(word), word) for word in fused_words],
        tuple(decisions),
        calls,
    )


def apply_repeated_span_identifier_crops(
    engine,
    image: Image.Image,
    table: TableLayout,
    raw_words: list[dict],
    fused_words: list[dict],
    span_decisions: tuple[SpanFusionDecision, ...],
    *,
    minimum_confidence: float = 80.0,
) -> tuple[list[dict], tuple[RepeatedSpanIdentifierDecision, ...], int]:
    recognize_segment = getattr(engine, "recognize_identifier_segment", None)
    if not callable(recognize_segment) or not span_decisions:
        return list(fused_words), (), 0
    replacements: dict[int, dict] = {}
    decisions = []
    calls = 0
    for span in span_decisions:
        fused_word = next(
            (
                word
                for word in fused_words
                if (bbox := word.get("bbox"))
                and len(bbox) == 4
                and table.y_lines[span.row]
                <= (bbox[1] + bbox[3]) / 2
                <= table.y_lines[span.row + 1]
                and "/" in str(word.get("text", ""))
            ),
            None,
        )
        if fused_word is None:
            continue
        parts = [part.strip() for part in str(fused_word["text"]).split("/")]
        if len(parts) != 5:
            continue
        label_words = re.findall(r"[A-Z]{3,}", parts[1])
        identifier_parts = parts[4].split("-")
        if not label_words or len(identifier_parts) != 3:
            continue
        label = label_words[-1]
        if identifier_parts[1] == label:
            continue
        raw_identifier = next(
            (
                word
                for word in _words_in_span(
                    raw_words,
                    (
                        table.x_lines[span.start_col],
                        table.y_lines[span.row],
                        table.x_lines[span.end_col + 1],
                        table.y_lines[span.row + 1],
                    ),
                )
                if str(word.get("text", "")).count("-") == 2
            ),
            None,
        )
        if raw_identifier is None:
            continue
        raw_text = str(raw_identifier["text"]).strip()
        raw_parts = raw_text.split("-")
        if len(raw_parts) != 3 or not raw_parts[1]:
            continue
        bbox = tuple(raw_identifier["bbox"])
        start = len(raw_parts[0]) + 1
        end = start + len(raw_parts[1])
        total = max(1, len(raw_text))
        crop_bbox = (
            max(0, bbox[0] + round((bbox[2] - bbox[0]) * start / total) - 4),
            max(0, bbox[1] - 6),
            min(
                image.width,
                bbox[0] + round((bbox[2] - bbox[0]) * end / total) + 4,
            ),
            min(image.height, bbox[3] + 6),
        )
        crop = image.crop(crop_bbox)
        try:
            crop_text, crop_confidence = recognize_segment(crop)
        finally:
            crop.close()
        calls += 1
        if crop_confidence < minimum_confidence or crop_text.strip().upper() != label:
            continue
        selected_identifier = "-".join(
            (identifier_parts[0], label, identifier_parts[2])
        )
        selected_text = " / ".join((*parts[:4], selected_identifier))
        replacements[id(fused_word)] = {
            **fused_word,
            "text": selected_text,
            "repeated_identifier_crop_bbox": crop_bbox,
        }
        decisions.append(
            RepeatedSpanIdentifierDecision(
                row=span.row,
                primary=parts[4],
                selected=selected_identifier,
                repeated_label=label,
                crop_confidence=crop_confidence,
                crop_bbox=crop_bbox,
            )
        )
    if not replacements:
        return list(fused_words), (), calls
    return (
        [replacements.get(id(word), word) for word in fused_words],
        tuple(decisions),
        calls,
    )
