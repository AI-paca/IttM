from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from app.chunking.vertical import (
    TableLayout,
    table_layout_to_rows,
    table_words_to_rows,
)
from app.recognition.cell_batches import recognize_missing_table_cell_batches
from app.recognition.contracts import TextOcrEngine

RECURSIVE_GRID_TABLE_KIND = "recursive_grid_table"
RECURSIVE_TABLE_CELL_OCR_MODES = frozenset({"off", "auto", "always"})
DIRECT_TABLE_CELL_OCR_MAX = 64
WIDE_NUMERIC_TABLE_MIN_COLS = 30
WIDE_NUMERIC_TABLE_MIN_ADDED_CELLS = 8
WIDE_NUMERIC_TABLE_MIN_RATIO = 0.15


@dataclass(frozen=True)
class TableCellRecognitionCandidate:
    rows: list[list[str]]
    calls: int
    coverage: float
    added_cells: int
    recovered_words: tuple[dict, ...]


def _is_numeric_cell_text(text: str) -> bool:
    value = text.strip()
    return bool(
        value
        and re.search(r"\d", value)
        and re.fullmatch(r"[\d\s.,%+:/()\-]+", value)
    )


def table_cell_candidate_numeric_ratio(
    candidate: TableCellRecognitionCandidate,
) -> float:
    if not candidate.recovered_words:
        return 0.0
    numeric_cells = sum(
        _is_numeric_cell_text(str(word.get("text", "")))
        for word in candidate.recovered_words
    )
    return numeric_cells / len(candidate.recovered_words)


def should_select_augmented_table_candidate(
    table: TableLayout,
    candidate: TableCellRecognitionCandidate,
    *,
    previous_coverage: float,
    augmented_coverage: float,
) -> tuple[bool, str]:
    if not candidate.recovered_words or augmented_coverage <= previous_coverage:
        return False, "no_coverage_gain"

    if (
        table.cols >= WIDE_NUMERIC_TABLE_MIN_COLS
        and candidate.added_cells >= WIDE_NUMERIC_TABLE_MIN_ADDED_CELLS
        and table_cell_candidate_numeric_ratio(candidate)
        < WIDE_NUMERIC_TABLE_MIN_RATIO
    ):
        return False, "wide_non_numeric"

    return True, "accepted"


def table_cell_psm(image: Image.Image) -> int:
    width, height = image.size
    return 7 if height <= 80 or width <= 180 else 6


def recognize_table_cell(engine: TextOcrEngine, image: Image.Image) -> str:
    return engine.recognize(
        image,
        mode="text_mode",
        psm=table_cell_psm(image),
    )


def recognize_table_cells(
    engine: TextOcrEngine,
    image: Image.Image,
    table: TableLayout,
    *,
    seed_rows: list[list[str]] | None = None,
    only_missing: bool = False,
    max_calls: int | None = None,
) -> tuple[list[list[str]], int]:
    cell_ocr_calls = 0

    def recognize_cell(cell_image: Image.Image) -> str:
        nonlocal cell_ocr_calls
        cell_ocr_calls += 1
        return recognize_table_cell(engine, cell_image)

    def contextual_recognize_cell(
        cell_image: Image.Image,
        cell,
        rows: list[list[str]],
    ) -> str:
        left = rows[cell.row][cell.col - 1] if cell.col > 0 else ""
        top = rows[cell.row - 1][cell.col] if cell.row > 0 else ""
        language_context = getattr(engine, "language_context", None)
        if callable(language_context):
            with language_context(left, top):
                return recognize_cell(cell_image)
        return recognize_cell(cell_image)

    return (
        table_layout_to_rows(
            image,
            table,
            recognize_cell,
            contextual_recognize_cell=contextual_recognize_cell,
            seed_rows=seed_rows,
            only_missing=only_missing,
            max_recognitions=max_calls,
        ),
        cell_ocr_calls,
    )


def table_row_cell_coverage(table: TableLayout, rows: list[list[str]]) -> float:
    if not table.cells:
        return 0.0
    populated_cells = sum(bool(cell.strip()) for row in rows for cell in row)
    return populated_cells / len(table.cells)


def table_word_cell_coverage(table: TableLayout, words: list[dict]) -> float:
    return table_row_cell_coverage(table, table_words_to_rows(table, words))


def recognize_table_cell_candidate(
    engine: TextOcrEngine,
    image: Image.Image,
    table: TableLayout,
    *,
    seed_words: list[dict] | None = None,
    max_batch_pixels: int = 8_000_000,
) -> TableCellRecognitionCandidate:
    seed_rows = table_words_to_rows(table, seed_words or [])
    if len(table.cells) > DIRECT_TABLE_CELL_OCR_MAX:
        recovered_words, calls = recognize_missing_table_cell_batches(
            engine,
            image,
            table,
            seed_words=seed_words,
            max_batch_pixels=max_batch_pixels,
        )
        rows = table_words_to_rows(
            table,
            [*(seed_words or []), *recovered_words],
        )
    else:
        rows, calls = recognize_table_cells(
            engine,
            image,
            table,
            seed_rows=seed_rows,
            only_missing=bool(seed_words),
        )
        recovered_words = []
        for cell in table.cells:
            seeded = seed_rows[cell.row][cell.col].strip()
            recovered = rows[cell.row][cell.col].strip()
            if seeded or not recovered:
                continue
            recovered_words.append(
                {
                    "text": recovered,
                    "bbox": cell.bbox,
                    "conf": 100,
                }
            )
    return TableCellRecognitionCandidate(
        rows=rows,
        calls=calls,
        coverage=table_row_cell_coverage(table, rows),
        added_cells=len(recovered_words),
        recovered_words=tuple(recovered_words),
    )


def is_recursive_grid_table(metadata: dict | None) -> bool:
    return (metadata or {}).get("layout_kind") == RECURSIVE_GRID_TABLE_KIND


def should_try_recursive_table_cells(
    *,
    metadata: dict | None,
    mode: str,
    cell_count: int,
    table_markdown: str,
    word_cell_coverage: float,
    min_word_cell_coverage: float,
) -> bool:
    if mode not in RECURSIVE_TABLE_CELL_OCR_MODES:
        known = ", ".join(sorted(RECURSIVE_TABLE_CELL_OCR_MODES))
        raise ValueError(f"Unknown recursive table cell OCR mode '{mode}'. Known modes: {known}")
    if mode == "off" or not is_recursive_grid_table(metadata):
        return False
    if cell_count <= 0:
        return False
    if mode == "always":
        return True
    return (
        not table_markdown.strip()
        or word_cell_coverage < min_word_cell_coverage
        or cell_count > DIRECT_TABLE_CELL_OCR_MAX
    )
