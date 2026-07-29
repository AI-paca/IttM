from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from app.chunking.vertical import TableLayout, table_words_to_rows

_ASCII_SEGMENT = re.compile(r"^[A-Z][A-Z0-9]*$|^[0-9]+$")


@dataclass(frozen=True)
class SegmentCropDecision:
    row: int
    column: int
    segment: int
    primary: str
    selected: str
    crop_confidence: float
    supporting_sources: tuple[str, ...]
    crop_bbox: tuple[int, int, int, int]


def _identifier_word_in_cell(
    table: TableLayout,
    words: list[dict],
    row: int,
    column: int,
) -> dict | None:
    left = table.x_lines[column]
    right = table.x_lines[column + 1]
    top = table.y_lines[row]
    bottom = table.y_lines[row + 1]
    candidates = [
        word
        for word in words
        if str(word.get("text", "")).count("-") >= 2
        and (bbox := word.get("bbox"))
        and len(bbox) == 4
        and left <= (bbox[0] + bbox[2]) / 2 <= right
        and top <= (bbox[1] + bbox[3]) / 2 <= bottom
    ]
    return max(
        candidates,
        key=lambda word: len(str(word.get("text", ""))),
        default=None,
    )


def _raw_segments(text: str) -> tuple[str, ...] | None:
    segments = tuple(text.strip().split("-"))
    if not 3 <= len(segments) <= 5:
        return None
    return segments


def _segment_crop_bbox(
    word_bbox: tuple[int, int, int, int],
    text: str,
    segment_index: int,
    image: Image.Image,
) -> tuple[int, int, int, int] | None:
    segments = text.split("-")
    if segment_index >= len(segments) or not segments[segment_index]:
        return None
    start = sum(len(segment) + 1 for segment in segments[:segment_index])
    end = start + len(segments[segment_index])
    total = max(1, len(text))
    left, top, right, bottom = word_bbox
    x1 = max(0, left + round((right - left) * start / total) - 4)
    x2 = min(image.width, left + round((right - left) * end / total) + 4)
    y1 = max(0, top - 6)
    y2 = min(image.height, bottom + 6)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _corroborated_alternatives(
    candidate_rows: dict[str, list[list[str]]],
    row: int,
    column: int,
    segment_index: int,
) -> dict[str, tuple[str, ...]]:
    sources: dict[str, list[str]] = {}
    for source, rows in candidate_rows.items():
        segments = _raw_segments(rows[row][column].strip())
        if segments is None or segment_index >= len(segments):
            continue
        value = segments[segment_index].strip().upper()
        if not _ASCII_SEGMENT.fullmatch(value):
            continue
        sources.setdefault(value, []).append(source)
    return {
        value: tuple(sorted(observed_sources))
        for value, observed_sources in sources.items()
        if len(observed_sources) >= 2
    }


def fuse_identifier_segment_crops(
    engine,
    image: Image.Image,
    table: TableLayout,
    primary_words: list[dict],
    candidate_passes: tuple[tuple[str, list[dict]], ...],
    *,
    excluded_rows: frozenset[int] = frozenset(),
    minimum_confidence: float = 80.0,
    max_calls: int = 64,
) -> tuple[list[dict], tuple[SegmentCropDecision, ...], int]:
    recognize_segment = getattr(engine, "recognize_identifier_segment", None)
    if not callable(recognize_segment) or not candidate_passes or max_calls <= 0:
        return list(primary_words), (), 0

    primary_rows = table_words_to_rows(table, primary_words)
    candidate_rows = {
        source: table_words_to_rows(table, words)
        for source, words in candidate_passes
    }
    replacements = []
    decisions = []
    replaced_cells = set()
    calls = 0
    for row in range(table.rows):
        if row in excluded_rows:
            continue
        for column in range(table.cols):
            word = _identifier_word_in_cell(
                table,
                primary_words,
                row,
                column,
            )
            if word is None:
                continue
            primary_text = str(word["text"]).strip()
            segments = _raw_segments(primary_text)
            if segments is None:
                continue
            selected_segments = list(segments)
            cell_decisions = []
            for segment_index in range(1, len(segments)):
                alternatives = _corroborated_alternatives(
                    candidate_rows,
                    row,
                    column,
                    segment_index,
                )
                if not alternatives:
                    continue
                crop_bbox = _segment_crop_bbox(
                    tuple(word["bbox"]),
                    primary_text,
                    segment_index,
                    image,
                )
                if crop_bbox is None or calls >= max_calls:
                    continue
                crop = image.crop(crop_bbox)
                try:
                    crop_text, crop_confidence = recognize_segment(crop)
                finally:
                    crop.close()
                calls += 1
                normalized = crop_text.strip().upper()
                supporting_sources = alternatives.get(normalized)
                if (
                    supporting_sources is None
                    or crop_confidence < minimum_confidence
                    or normalized == segments[segment_index].upper()
                ):
                    continue
                selected_segments[segment_index] = normalized
                cell_decisions.append(
                    SegmentCropDecision(
                        row=row,
                        column=column,
                        segment=segment_index,
                        primary=segments[segment_index],
                        selected=normalized,
                        crop_confidence=crop_confidence,
                        supporting_sources=supporting_sources,
                        crop_bbox=crop_bbox,
                    )
                )
            if not cell_decisions:
                continue
            selected_text = "-".join(selected_segments)
            replacements.append(
                {
                    **word,
                    "text": selected_text,
                    "segment_crop_sources": tuple(
                        decision.supporting_sources for decision in cell_decisions
                    ),
                }
            )
            decisions.extend(cell_decisions)
            replaced_cells.add((row, column))

    if not replacements:
        return list(primary_words), (), calls
    kept = []
    for word in primary_words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            kept.append(word)
            continue
        x = (bbox[0] + bbox[2]) / 2
        y = (bbox[1] + bbox[3]) / 2
        if any(
            table.x_lines[column] <= x <= table.x_lines[column + 1]
            and table.y_lines[row] <= y <= table.y_lines[row + 1]
            for row, column in replaced_cells
        ):
            continue
        kept.append(word)
    return [*kept, *replacements], tuple(decisions), calls
