from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from app.chunking.vertical import (
    TableLayout,
    _curriculum_summary_grid_to_markdown,
    _curriculum_title_page_grid_to_markdown,
    _normalize_known_table_columns,
    table_words_to_rows,
)

MERGE_LEFT = "::merge-left::"
MERGE_UP = "::merge-up::"
MERGE_UP_LEFT = "::merge-up-left::"
MERGE_MARKERS = {MERGE_LEFT, MERGE_UP, MERGE_UP_LEFT}
LINE_MERGE_MODE = "line_merge_v1"
RECURSIVE_GAPS_MODE = "recursive_gaps_v1"


@dataclass(frozen=True)
class _SlotWord:
    text: str
    bbox: tuple[int, int, int, int]

    @property
    def x_center(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def y_center(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


def table_words_to_slot_markdown(
    image: Image.Image,
    table: TableLayout,
    words: list[dict],
    *,
    mode: str = "line_merge_v1",
) -> str:
    rows = table_words_to_slot_rows(image, table, words, mode=mode)
    visible_rows = [["" if cell in MERGE_MARKERS else cell for cell in row] for row in rows]
    return _slot_rows_to_markdown(visible_rows)


def _slot_rows_to_markdown(rows: list[list[str]]) -> str:
    title_markdown = _curriculum_title_page_grid_to_markdown(rows)
    if title_markdown:
        return title_markdown

    summary_markdown = _curriculum_summary_grid_to_markdown(rows)
    if summary_markdown:
        return summary_markdown

    visible = [row for row in rows if any(cell.strip() for cell in row)]
    if not visible:
        return ""
    width = max(len(row) for row in visible)
    padded = [row + [""] * (width - len(row)) for row in visible]
    padded = _normalize_known_table_columns(padded)
    separator = ["---" for _ in range(width)]
    body = padded[1:] or [[" " for _ in range(width)]]
    return "\n".join("| " + " | ".join(row) + " |" for row in (padded[0], separator, *body))


def table_words_to_slot_rows(
    image: Image.Image,
    table: TableLayout,
    words: list[dict],
    *,
    mode: str = "line_merge_v1",
) -> list[list[str]]:
    rows = table_words_to_rows(table, words)
    if mode == "off":
        return rows
    known_modes = {LINE_MERGE_MODE, RECURSIVE_GAPS_MODE}
    if mode not in known_modes:
        known = ", ".join(sorted(known_modes))
        raise ValueError(f"Unknown table slot builder '{mode}'. Known modes: {known}")

    line_map = _LineMap.from_image(image)
    if mode == RECURSIVE_GAPS_MODE:
        return _recursive_gap_slot_rows(table, words, line_map)

    rows = _merge_horizontal_slots(rows, table, line_map)
    rows = _merge_vertical_slots(rows, table, line_map)
    return rows


def words_to_recursive_slot_markdown(
    words: list[dict],
    *,
    max_cols: int | None = 8,
    min_density: float = 0.35,
) -> str:
    slot_words = _slot_words(words)
    if len(slot_words) < 8:
        return ""

    rows_by_band = _word_rows(slot_words)
    if len(rows_by_band) < 3:
        return ""

    x_lines = _recursive_word_x_lines(rows_by_band)
    if len(x_lines) < 4:
        return ""
    column_count = len(x_lines) - 1
    if max_cols is not None and column_count > max_cols:
        return ""
    if column_count > 4 and (column_count < 8 or len(rows_by_band) < 10):
        return ""

    rows = _words_to_virtual_grid(rows_by_band, x_lines)
    populated_cell_count = sum(1 for row in rows for cell in row if cell.strip() and cell.strip() not in MERGE_MARKERS)
    total_cell_count = max(1, len(rows) * (len(x_lines) - 1))
    if populated_cell_count / total_cell_count < min_density:
        return ""

    populated_rows = [
        row for row in rows if sum(1 for cell in row if cell.strip() and cell.strip() not in MERGE_MARKERS) >= 2
    ]
    if len(populated_rows) < max(3, int(np.ceil(len(rows) * 0.45))):
        return ""

    return _slot_rows_to_markdown([["" if cell in MERGE_MARKERS else cell for cell in row] for row in rows])


class _LineMap:
    def __init__(self, dark: np.ndarray):
        self.dark = dark
        self.height, self.width = dark.shape[:2]

    @classmethod
    def from_image(cls, image: Image.Image) -> "_LineMap":
        gray = np.array(image.convert("L"))
        return cls(gray < 185)

    def vertical_coverage(self, x: int, top: int, bottom: int) -> float:
        top = max(0, min(self.height, top))
        bottom = max(0, min(self.height, bottom))
        if bottom <= top:
            return 0.0
        left = max(0, min(self.width, int(x) - 2))
        right = max(0, min(self.width, int(x) + 3))
        if right <= left:
            return 0.0
        strip = self.dark[top:bottom, left:right]
        return float(np.mean(np.any(strip, axis=1)))

    def horizontal_coverage(self, y: int, left: int, right: int) -> float:
        left = max(0, min(self.width, left))
        right = max(0, min(self.width, right))
        if right <= left:
            return 0.0
        top = max(0, min(self.height, int(y) - 2))
        bottom = max(0, min(self.height, int(y) + 3))
        if bottom <= top:
            return 0.0
        strip = self.dark[top:bottom, left:right]
        return float(np.mean(np.any(strip, axis=0)))


def _merge_horizontal_slots(
    rows: list[list[str]],
    table: TableLayout,
    line_map: _LineMap,
) -> list[list[str]]:
    if table.cols <= 1:
        return rows

    merged = [list(row) for row in rows]
    for row_index, row in enumerate(merged):
        if row_index + 1 >= len(table.y_lines):
            continue
        top = table.y_lines[row_index]
        bottom = table.y_lines[row_index + 1]
        segments = _row_segments_without_vertical_lines(table, line_map, row_index, top, bottom)
        for start, end in segments:
            if end <= start:
                continue
            cells = row[start : end + 1]
            non_empty = [
                index for index, cell in enumerate(cells) if cell.strip() and cell.strip() not in MERGE_MARKERS
            ]
            if not non_empty:
                continue
            if not _should_merge_horizontal_segment(cells):
                continue

            text = " ".join(cell.strip() for cell in cells if cell.strip() and cell.strip() not in MERGE_MARKERS)
            row[start] = text
            for col in range(start + 1, end + 1):
                row[col] = MERGE_LEFT
    return merged


def _row_segments_without_vertical_lines(
    table: TableLayout,
    line_map: _LineMap,
    row_index: int,
    top: int,
    bottom: int,
) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start = 0
    pad = max(2, min(8, (bottom - top) // 8))
    for boundary_col in range(1, table.cols):
        x = table.x_lines[boundary_col]
        coverage = line_map.vertical_coverage(x, top + pad, bottom - pad)
        if coverage >= _vertical_boundary_threshold(table, row_index):
            segments.append((start, boundary_col - 1))
            start = boundary_col
    segments.append((start, table.cols - 1))
    return segments


def table_has_horizontal_slot_merges(
    image: Image.Image,
    table: TableLayout,
) -> bool:
    if table.cols <= 1 or table.rows <= 1:
        return False
    line_map = _LineMap.from_image(image)
    saw_split_row = False
    saw_merged_row = False
    for row_index in range(table.rows):
        top = table.y_lines[row_index]
        bottom = table.y_lines[row_index + 1]
        segments = _row_segments_without_vertical_lines(
            table,
            line_map,
            row_index,
            top,
            bottom,
        )
        if len(segments) == table.cols:
            saw_split_row = True
        if any(end > start for start, end in segments):
            saw_merged_row = True
    return saw_split_row and saw_merged_row


def _vertical_boundary_threshold(table: TableLayout, row_index: int) -> float:
    # Header and section rows often have shorter visible strokes after scans.
    # Keep the threshold permissive, but require a real line for a hard split.
    if row_index == 0:
        return 0.35
    return 0.42


def _should_merge_horizontal_segment(cells: list[str]) -> bool:
    if len(cells) <= 1:
        return False
    populated = [cell.strip() for cell in cells if cell.strip() and cell.strip() not in MERGE_MARKERS]
    if not populated:
        return False
    if len(populated) == 1:
        return True
    joined = " ".join(populated)
    compact_len = len("".join(joined.split()))
    return len(populated) <= max(2, len(cells) // 2) and compact_len >= 8


def _merge_vertical_slots(
    rows: list[list[str]],
    table: TableLayout,
    line_map: _LineMap,
) -> list[list[str]]:
    if table.rows <= 1:
        return rows

    merged = [list(row) for row in rows]
    for row_index in range(1, min(len(merged), table.rows)):
        for col_index in range(min(len(merged[row_index]), table.cols)):
            if merged[row_index][col_index].strip():
                continue
            if col_index + 1 >= len(table.x_lines):
                continue
            left = table.x_lines[col_index]
            right = table.x_lines[col_index + 1]
            y = table.y_lines[row_index]
            pad = max(2, min(8, (right - left) // 8))
            coverage = line_map.horizontal_coverage(y, left + pad, right - pad)
            if coverage >= 0.40:
                continue
            above = merged[row_index - 1][col_index]
            if not above.strip() and above not in {MERGE_UP, MERGE_UP_LEFT, MERGE_LEFT}:
                continue
            merged[row_index][col_index] = (
                MERGE_UP_LEFT if col_index > 0 and merged[row_index][col_index - 1] == MERGE_UP else MERGE_UP
            )
    return merged


def _recursive_gap_slot_rows(
    table: TableLayout,
    words: list[dict],
    line_map: _LineMap,
) -> list[list[str]]:
    slot_words = _slot_words(words)
    if not slot_words:
        return table_words_to_rows(table, words)

    rows_by_band: list[list[_SlotWord]] = [[] for _ in range(table.rows)]
    for word in slot_words:
        row = _position_to_interval(word.y_center, table.y_lines)
        if row is None or row >= table.rows:
            continue
        rows_by_band[row].append(word)

    x_lines = _recursive_gap_x_lines(table, rows_by_band)
    if len(x_lines) < 2:
        return table_words_to_rows(table, words)

    rows: list[list[str]] = []
    for row_index, row_words in enumerate(rows_by_band):
        row = ["" for _ in range(len(x_lines) - 1)]
        merge_cells: set[int] = set()
        for word in sorted(row_words, key=lambda item: (item.bbox[0], item.bbox[1])):
            start_col = _position_to_interval(word.x_center, x_lines)
            if start_col is None:
                continue
            span_start, span_end = _word_span_columns(word, x_lines, row_index, table, line_map)
            start_col = min(start_col, span_start)
            row[start_col] = " ".join(part for part in (row[start_col], word.text) if part).strip()
            for col in range(start_col + 1, span_end + 1):
                merge_cells.add(col)
        for col in sorted(merge_cells):
            if not row[col].strip():
                row[col] = MERGE_LEFT
        rows.append(row)

    if table.cols <= 1:
        return rows
    return _merge_horizontal_slots(
        rows,
        _table_with_x_lines(table, x_lines),
        line_map,
    )


def _slot_words(words: list[dict]) -> list[_SlotWord]:
    result: list[_SlotWord] = []
    for word in words:
        text = _clean_word(str(word.get("text", ""))).replace("|", "\\|")
        bbox = word.get("bbox")
        if not text or not bbox or len(bbox) != 4:
            continue
        left, top, right, bottom = (int(value) for value in bbox)
        if right <= left or bottom <= top:
            continue
        result.append(_SlotWord(text=text, bbox=(left, top, right, bottom)))
    return result


def _clean_word(text: str) -> str:
    return " ".join(text.strip().strip("|[]'\"‘’“”").split())


def _position_to_interval(position: float, lines: tuple[int, ...]) -> int | None:
    if len(lines) < 2 or position < lines[0] or position > lines[-1]:
        return None
    if position == lines[-1]:
        return len(lines) - 2
    for index, (start, end) in enumerate(zip(lines, lines[1:])):
        if start <= position < end:
            return index
    return None


def _recursive_gap_x_lines(
    table: TableLayout,
    rows_by_band: list[list[_SlotWord]],
) -> tuple[int, ...]:
    left = int(table.x_lines[0])
    right = int(table.x_lines[-1])
    candidates = [*table.x_lines]
    min_gap = _recursive_gap_threshold(table, rows_by_band)
    min_virtual_width = _minimum_virtual_split_width(table)
    for row_words in rows_by_band:
        candidates.extend(
            cut
            for cut in _recursive_gap_cuts(row_words, left, right, min_gap)
            if _base_slot_width(cut, table.x_lines) >= min_virtual_width
        )
    return _merge_lines(candidates, tolerance=max(3, int((right - left) * 0.003)))


def _word_rows(words: list[_SlotWord]) -> list[list[_SlotWord]]:
    heights = [word.bbox[3] - word.bbox[1] for word in words]
    y_tolerance = max(6, int(round(float(np.median(heights)) * 0.72))) if heights else 8
    rows: list[list[_SlotWord]] = []
    centers: list[float] = []
    for word in sorted(words, key=lambda item: (item.y_center, item.bbox[0])):
        if not rows or abs(word.y_center - centers[-1]) > y_tolerance:
            rows.append([word])
            centers.append(word.y_center)
            continue
        rows[-1].append(word)
        centers[-1] = (centers[-1] * (len(rows[-1]) - 1) + word.y_center) / len(rows[-1])

    return [sorted(row, key=lambda item: item.bbox[0]) for row in rows]


def _recursive_word_x_lines(rows_by_band: list[list[_SlotWord]]) -> tuple[int, ...]:
    all_words = [word for row in rows_by_band for word in row]
    left = min(word.bbox[0] for word in all_words)
    right = max(word.bbox[2] for word in all_words)
    table_width = max(1, right - left)
    widths = [word.bbox[2] - word.bbox[0] for word in all_words]
    heights = [word.bbox[3] - word.bbox[1] for word in all_words]
    median_width = float(np.median(widths)) if widths else 24.0
    median_height = float(np.median(heights)) if heights else 12.0
    min_gap = int(max(16, median_width * 0.55, median_height * 1.2, table_width * 0.012))
    candidates = [left, right]
    for row_words in rows_by_band:
        candidates.extend(_recursive_gap_cuts(row_words, left, right, min_gap))
    return _merge_lines(candidates, tolerance=max(3, int(table_width * 0.003)))


def _words_to_virtual_grid(
    rows_by_band: list[list[_SlotWord]],
    x_lines: tuple[int, ...],
) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_words in rows_by_band:
        row = ["" for _ in range(len(x_lines) - 1)]
        merge_cells: set[int] = set()
        for word in row_words:
            center_col = _position_to_interval(word.x_center, x_lines)
            if center_col is None:
                continue
            row[center_col] = " ".join(part for part in (row[center_col], word.text) if part).strip()
            for boundary_col in range(center_col + 1, len(x_lines) - 1):
                x = x_lines[boundary_col]
                if word.bbox[0] + 1 < x < word.bbox[2] - 1:
                    merge_cells.add(boundary_col)
        for col in sorted(merge_cells):
            if not row[col].strip():
                row[col] = MERGE_LEFT
        rows.append(row)
    return rows


def _recursive_gap_threshold(
    table: TableLayout,
    rows_by_band: list[list[_SlotWord]],
) -> int:
    widths = [word.bbox[2] - word.bbox[0] for row in rows_by_band for word in row]
    heights = [word.bbox[3] - word.bbox[1] for row in rows_by_band for word in row]
    median_width = float(np.median(widths)) if widths else 12.0
    median_height = float(np.median(heights)) if heights else 12.0
    table_width = max(1, table.x_lines[-1] - table.x_lines[0])
    return int(max(12, median_width * 0.55, median_height * 1.2, table_width * 0.01))


def _recursive_gap_cuts(
    row_words: list[_SlotWord],
    left: int,
    right: int,
    min_gap: int,
) -> list[int]:
    words = sorted(
        (word for word in row_words if word.bbox[2] > left and word.bbox[0] < right),
        key=lambda word: (word.bbox[0], word.bbox[2]),
    )
    if len(words) < 2:
        return []

    gaps: list[tuple[int, int]] = []
    for previous, current in zip(words, words[1:]):
        gap = current.bbox[0] - previous.bbox[2]
        if gap >= min_gap:
            gaps.append((gap, current.bbox[0]))
    if not gaps:
        return []

    _, cut = max(gaps, key=lambda item: item[0])
    left_words = [word for word in words if word.bbox[2] <= cut]
    right_words = [word for word in words if word.bbox[0] >= cut]
    return [
        *_recursive_gap_cuts(left_words, left, cut, min_gap),
        cut,
        *_recursive_gap_cuts(right_words, cut, right, min_gap),
    ]


def _minimum_virtual_split_width(table: TableLayout) -> int:
    widths = [right - left for left, right in zip(table.x_lines, table.x_lines[1:])]
    median_width = float(np.median(widths)) if widths else 0.0
    if table.cols <= 2:
        return int(max(80, median_width * 0.45))
    return int(max(120, median_width * 1.6))


def _base_slot_width(cut: int, x_lines: tuple[int, ...]) -> int:
    interval = _position_to_interval(cut, x_lines)
    if interval is None:
        return 0
    return x_lines[interval + 1] - x_lines[interval]


def _merge_lines(lines: list[int] | tuple[int, ...], *, tolerance: int) -> tuple[int, ...]:
    ordered = sorted(set(int(value) for value in lines))
    if not ordered:
        return ()
    groups: list[list[int]] = [[ordered[0]]]
    for line in ordered[1:]:
        if line - groups[-1][-1] <= tolerance:
            groups[-1].append(line)
        else:
            groups.append([line])
    return tuple(int(round(sum(group) / len(group))) for group in groups)


def _table_with_x_lines(table: TableLayout, x_lines: tuple[int, ...]) -> TableLayout:
    return TableLayout(
        bbox=table.bbox,
        rows=table.rows,
        cols=len(x_lines) - 1,
        x_lines=x_lines,
        y_lines=table.y_lines,
        cells=table.cells,
    )


def _word_span_columns(
    word: _SlotWord,
    x_lines: tuple[int, ...],
    row_index: int,
    table: TableLayout,
    line_map: _LineMap,
) -> tuple[int, int]:
    start_col = _position_to_interval(max(word.bbox[0], x_lines[0]), x_lines)
    end_col = _position_to_interval(min(word.bbox[2], x_lines[-1]), x_lines)
    center_col = _position_to_interval(word.x_center, x_lines)
    if center_col is None:
        return 0, 0
    start = center_col if start_col is None else min(center_col, start_col)
    end = center_col if end_col is None else max(center_col, end_col)
    if end <= start:
        return start, start

    crossing_end = start
    top, bottom = _row_bounds(table, row_index)
    for boundary_col in range(start + 1, end + 1):
        x = x_lines[boundary_col]
        if word.bbox[0] + 1 < x < word.bbox[2] - 1:
            crossing_end = boundary_col
            continue
        if line_map.vertical_coverage(x, top, bottom) < 0.08 and word.bbox[0] <= x <= word.bbox[2]:
            crossing_end = boundary_col
    return start, max(start, crossing_end)


def _row_bounds(table: TableLayout, row_index: int) -> tuple[int, int]:
    if row_index + 1 >= len(table.y_lines):
        return table.y_lines[0], table.y_lines[-1]
    return table.y_lines[row_index], table.y_lines[row_index + 1]
