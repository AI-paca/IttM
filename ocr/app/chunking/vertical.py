import re
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class TableCell:
    row: int
    col: int
    bbox: Box


@dataclass(frozen=True)
class TableLayout:
    bbox: Box
    rows: int
    cols: int
    x_lines: tuple[int, ...]
    y_lines: tuple[int, ...]
    cells: tuple[TableCell, ...]


@dataclass(frozen=True)
class LayoutRegion:
    kind: str
    image: Image.Image
    bbox: Box
    table: TableLayout | None = None


def remove_white_borders(image: Image.Image, bg_threshold: int = 240) -> Image.Image:
    """
    Crops out empty white borders from the image to help horizontal projection
    when there's content on one side and empty space on the other.
    """
    if image.mode != "L":
        gray = image.convert("L")
    else:
        gray = image
    img_arr = np.array(gray)
    mask = img_arr < bg_threshold
    coords = np.argwhere(mask)
    if coords.size == 0:
        return image
    y_min, x_min = coords.min(axis=0)
    y_max, x_max = coords.max(axis=0)
    pad = 10
    x_min = max(0, x_min - pad)
    y_min = max(0, y_min - pad)
    x_max = min(image.size[0], x_max + pad)
    y_max = min(image.size[1], y_max + pad)
    return image.crop((x_min, y_min, x_max, y_max))


def _has_visible_content(image: Image.Image, bg_threshold: int = 245) -> bool:
    gray = np.array(image.convert("L"))
    return bool(np.mean(gray < bg_threshold) > 0.0005)


def _group_indexes(indexes: np.ndarray, max_gap: int = 2) -> list[tuple[int, int]]:
    if indexes.size == 0:
        return []

    groups = []
    start = int(indexes[0])
    previous = int(indexes[0])

    for raw_index in indexes[1:]:
        index = int(raw_index)
        if index - previous <= max_gap:
            previous = index
            continue
        groups.append((start, previous))
        start = index
        previous = index

    groups.append((start, previous))
    return groups


def _merge_positions(positions: list[int], tolerance: int = 5) -> list[int]:
    if not positions:
        return []

    sorted_positions = sorted(int(position) for position in positions)
    groups: list[list[int]] = [[sorted_positions[0]]]

    for position in sorted_positions[1:]:
        if position - groups[-1][-1] <= tolerance:
            groups[-1].append(position)
            continue
        groups.append([position])

    return [int(round(sum(group) / len(group))) for group in groups]


def _line_positions(mask: np.ndarray, *, axis: int, min_coverage: int) -> list[int]:
    projection = np.count_nonzero(mask > 0, axis=axis)
    indexes = np.where(projection >= min_coverage)[0]
    return [int(round((start + end) / 2)) for start, end in _group_indexes(indexes)]


def _with_edges(positions: list[int], size: int, tolerance: int = 8) -> tuple[int, ...]:
    if size <= 1:
        return tuple(sorted(set(positions)))

    result = _merge_positions(list(positions), tolerance=max(2, tolerance // 2))
    if not any(pos <= tolerance for pos in result):
        result.append(0)
    if not any(pos >= size - 1 - tolerance for pos in result):
        result.append(size - 1)
    return tuple(
        _merge_positions(
            [max(0, min(size - 1, pos)) for pos in result], tolerance=tolerance
        )
    )


def _cells_from_lines(
    x_lines: tuple[int, ...], y_lines: tuple[int, ...]
) -> tuple[TableCell, ...]:
    cells = []
    for row, (top, bottom) in enumerate(zip(y_lines, y_lines[1:])):
        for col, (left, right) in enumerate(zip(x_lines, x_lines[1:])):
            if right - left < 8 or bottom - top < 8:
                continue
            cells.append(TableCell(row=row, col=col, bbox=(left, top, right, bottom)))
    return tuple(cells)


def _intersection_over_union(left: Box, right: Box) -> float:
    lx1, ly1, lx2, ly2 = left
    rx1, ry1, rx2, ry2 = right
    x1 = max(lx1, rx1)
    y1 = max(ly1, ry1)
    x2 = min(lx2, rx2)
    y2 = min(ly2, ry2)

    if x2 <= x1 or y2 <= y1:
        return 0.0

    intersection = (x2 - x1) * (y2 - y1)
    left_area = max(0, lx2 - lx1) * max(0, ly2 - ly1)
    right_area = max(0, rx2 - rx1) * max(0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _dedupe_tables(tables: list[TableLayout]) -> list[TableLayout]:
    selected: list[TableLayout] = []
    for table in sorted(
        tables,
        key=lambda t: (-(t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]), t.bbox[1]),
    ):
        if any(
            _intersection_over_union(table.bbox, existing.bbox) > 0.65
            for existing in selected
        ):
            continue
        selected.append(table)
    return sorted(selected, key=lambda t: (t.bbox[1], t.bbox[0]))


def _foreground_mask(gray: np.ndarray):
    import cv2

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    block_size = max(15, min(51, (min(gray.shape[:2]) // 24) | 1))
    adaptive = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        11,
    )

    ink_ratio = float(np.mean(otsu > 0))
    if 0.0005 <= ink_ratio <= 0.25:
        return cv2.bitwise_or(otsu, adaptive)
    return otsu


def _grid_line_masks(binary: np.ndarray):
    import cv2

    height, width = binary.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(18, width // 28), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(18, height // 28))
    )
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1
    )
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)

    horizontal = cv2.dilate(
        horizontal, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1)), iterations=1
    )
    vertical = cv2.dilate(
        vertical, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3)), iterations=1
    )
    return horizontal, vertical


def _cell_line_hints_from_contours(
    table_grid: np.ndarray, local_width: int, local_height: int
) -> tuple[list[int], list[int], int]:
    """
    Use contour holes inside the connected grid as additional cell-boundary hints.

    Projection catches long straight lines well; contour hints help when scans
    have small breaks, thicker borders, or merged header cells.
    """
    import cv2

    x_positions: list[int] = []
    y_positions: list[int] = []
    confirmed_cells = 0

    contours, hierarchy = cv2.findContours(
        table_grid, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None:
        return x_positions, y_positions, confirmed_cells

    min_cell_width = max(6, int(local_width * 0.002))
    min_cell_height = max(6, int(local_height * 0.002))
    max_cell_area = int(local_width * local_height * 0.95)

    for index, contour in enumerate(contours):
        x, y, width, height = cv2.boundingRect(contour)
        area = width * height
        if width < min_cell_width or height < min_cell_height:
            continue
        if area >= max_cell_area:
            continue

        # Contours returned for cell interiors sit just inside the grid lines.
        # Their edges are still valuable as near-line positions after clustering.
        x_positions.extend([x, x + width])
        y_positions.extend([y, y + height])

        if hierarchy[0][index][3] >= 0:
            contour_area = cv2.contourArea(contour)
            rectangularity = contour_area / area if area else 0.0
            if rectangularity >= 0.82:
                confirmed_cells += 1

    return x_positions, y_positions, confirmed_cells


def _build_grid_mask(horizontal: np.ndarray, vertical: np.ndarray):
    import cv2

    grid = cv2.add(horizontal, vertical)
    grid = cv2.morphologyEx(
        grid,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    return cv2.dilate(
        grid, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1
    )


def detect_table_layouts(
    image: Image.Image,
    min_cells: int = 4,
    min_confirmed_cell_ratio: float = 0.0,
) -> list[TableLayout]:
    """
    Detect table-like grids using OpenCV line morphology and contours.

    This keeps schedule/form pages away from blind vertical slicing: once a table
    grid is detected, callers can OCR isolated cells and rebuild Markdown tables.
    """
    try:
        import cv2
    except Exception:
        return []

    gray = np.array(image.convert("L"))
    height, width = gray.shape[:2]
    if width < 80 or height < 60:
        return []

    binary = _foreground_mask(gray)
    horizontal, vertical = _grid_line_masks(binary)
    grid = _build_grid_mask(horizontal, vertical)

    contour_grid = cv2.morphologyEx(
        grid,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)),
        iterations=2,
    )
    contours, _ = cv2.findContours(
        contour_grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    image_area = width * height
    tables: list[TableLayout] = []

    for contour in contours:
        x, y, table_width, table_height = cv2.boundingRect(contour)
        table_area = table_width * table_height
        if table_width < 100 or table_height < 70:
            continue
        if table_area < max(6_000, int(image_area * 0.015)):
            continue

        pad = 2
        x = max(0, x - pad)
        y = max(0, y - pad)
        x2 = min(width, x + table_width + pad * 2)
        y2 = min(height, y + table_height + pad * 2)
        table_horizontal = horizontal[y:y2, x:x2]
        table_vertical = vertical[y:y2, x:x2]
        table_grid = grid[y:y2, x:x2]
        local_width = x2 - x
        local_height = y2 - y
        contour_x_lines, contour_y_lines, confirmed_cells = (
            _cell_line_hints_from_contours(
                table_grid,
                local_width,
                local_height,
            )
        )

        y_lines_local = _with_edges(
            [
                *_line_positions(
                    table_horizontal,
                    axis=1,
                    min_coverage=max(18, int(local_width * 0.22)),
                ),
                *contour_y_lines,
            ],
            local_height,
        )
        x_lines_local = _with_edges(
            [
                *_line_positions(
                    table_vertical,
                    axis=0,
                    min_coverage=max(18, int(local_height * 0.22)),
                ),
                *contour_x_lines,
            ],
            local_width,
        )

        if len(x_lines_local) < 3 or len(y_lines_local) < 3:
            continue
        if (
            local_width / max(1, len(x_lines_local) - 1) < 6
            or local_height / max(1, len(y_lines_local) - 1) < 6
        ):
            continue

        x_lines = tuple(x + pos for pos in x_lines_local)
        y_lines = tuple(y + pos for pos in y_lines_local)
        cells = _cells_from_lines(x_lines, y_lines)
        rows = len(y_lines) - 1
        cols = len(x_lines) - 1
        if rows * cols < min_cells or len(cells) < min_cells:
            continue
        if min_confirmed_cell_ratio > 0:
            min_confirmed_cells = max(
                min_cells,
                int(np.ceil(rows * cols * min_confirmed_cell_ratio)),
            )
            if confirmed_cells < min_confirmed_cells:
                continue

        tables.append(
            TableLayout(
                bbox=(x, y, x2, y2),
                rows=rows,
                cols=cols,
                x_lines=x_lines,
                y_lines=y_lines,
                cells=cells,
            )
        )

    return _dedupe_tables(tables)


def shift_table_layout(table: TableLayout, dx: int, dy: int) -> TableLayout:
    x1, y1, x2, y2 = table.bbox
    return TableLayout(
        bbox=(x1 + dx, y1 + dy, x2 + dx, y2 + dy),
        rows=table.rows,
        cols=table.cols,
        x_lines=tuple(x + dx for x in table.x_lines),
        y_lines=tuple(y + dy for y in table.y_lines),
        cells=tuple(
            TableCell(
                row=cell.row,
                col=cell.col,
                bbox=(
                    cell.bbox[0] + dx,
                    cell.bbox[1] + dy,
                    cell.bbox[2] + dx,
                    cell.bbox[3] + dy,
                ),
            )
            for cell in table.cells
        ),
    )


def logical_table_layout(
    image: Image.Image, table: TableLayout, min_major_coverage: float = 0.72
) -> TableLayout:
    """
    Collapse partial indentation lines into logical columns.

    Some curriculum-style tables use short vertical strokes inside the first
    column to show hierarchy. Those are real pixels, but not real Markdown
    columns. Keep only vertical separators that span most of the table height.
    """
    if table.cols <= 3:
        return table

    try:
        gray = np.array(image.convert("L"))
        binary = _foreground_mask(gray)
        _, vertical = _grid_line_masks(binary)
    except Exception:
        return table

    height, width = vertical.shape[:2]
    if height <= 0 or width <= 0:
        return table

    projection = np.count_nonzero(vertical > 0, axis=0)
    major_lines = []
    line_coverages = {}
    for position in table.x_lines:
        local = max(0, min(width - 1, int(position)))
        left = max(0, local - 3)
        right = min(width, local + 4)
        coverage = (
            float(np.max(projection[left:right])) / height if right > left else 0.0
        )
        line_coverages[int(position)] = coverage
        if coverage >= min_major_coverage:
            major_lines.append(local)

    major_lines = _merge_positions(major_lines, tolerance=8)
    if len(major_lines) < 2:
        return table

    edge_tolerance = max(12, int(width * 0.015))
    first_line = int(table.x_lines[0]) if table.x_lines else 0
    left_edge = (
        first_line
        if table.x_lines
        and (
            first_line <= edge_tolerance
            or line_coverages.get(first_line, 0.0) >= min_major_coverage
        )
        else 0
    )
    last_line = int(table.x_lines[-1]) if table.x_lines else width - 1
    right_edge = (
        last_line
        if table.x_lines
        and (
            last_line >= width - edge_tolerance
            or line_coverages.get(last_line, 0.0) >= min_major_coverage
        )
        else width - 1
    )

    min_column_width = max(20, int(width * 0.015))
    logical_x_lines = [max(0, min(width - 1, left_edge))]
    for position in major_lines:
        if position - logical_x_lines[-1] >= min_column_width:
            logical_x_lines.append(position)

    if right_edge - logical_x_lines[-1] >= min_column_width:
        logical_x_lines.append(max(0, min(width - 1, right_edge)))

    logical_x_lines = _merge_positions(logical_x_lines, tolerance=8)
    if len(logical_x_lines) < 3 or len(logical_x_lines) >= len(table.x_lines):
        return table

    x_lines = tuple(logical_x_lines)
    return TableLayout(
        bbox=table.bbox,
        rows=table.rows,
        cols=len(x_lines) - 1,
        x_lines=x_lines,
        y_lines=table.y_lines,
        cells=_cells_from_lines(x_lines, table.y_lines),
    )


def analyze_document_layout(
    image: Image.Image,
    min_confirmed_cell_ratio: float = 0.0,
) -> list[LayoutRegion]:
    """
    Split a page into ordered text/image and table regions.

    Table regions are returned as isolated crops with a table layout relative to
    that crop. If no tables are found, callers can keep using legacy chunking.
    """
    cropped = remove_white_borders(image)
    width, height = cropped.size
    tables = detect_table_layouts(
        cropped,
        min_confirmed_cell_ratio=min_confirmed_cell_ratio,
    )
    if not tables:
        return [LayoutRegion(kind="image", image=cropped, bbox=(0, 0, width, height))]

    regions: list[LayoutRegion] = []
    cursor_y = 0

    for table in tables:
        x1, y1, x2, y2 = table.bbox
        if y1 - cursor_y > 40:
            text_crop = cropped.crop((0, cursor_y, width, y1))
            if _has_visible_content(text_crop):
                regions.append(
                    LayoutRegion(
                        kind="image", image=text_crop, bbox=(0, cursor_y, width, y1)
                    )
                )

        table_crop = cropped.crop(table.bbox)
        regions.append(
            LayoutRegion(
                kind="table",
                image=table_crop,
                bbox=table.bbox,
                table=shift_table_layout(table, -x1, -y1),
            )
        )
        cursor_y = max(cursor_y, y2)

    if height - cursor_y > 40:
        text_crop = cropped.crop((0, cursor_y, width, height))
        if _has_visible_content(text_crop):
            regions.append(
                LayoutRegion(
                    kind="image", image=text_crop, bbox=(0, cursor_y, width, height)
                )
            )

    return regions


def _normalize_cell_text(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _clean_ocr_word(text: str) -> str:
    cleaned = text.strip().strip("|[]'\"‘’“”")
    return cleaned.strip()


def erase_table_lines_for_ocr(image: Image.Image) -> Image.Image:
    try:
        import cv2
    except Exception:
        return image

    rgb = np.array(image.convert("RGB"))
    gray = np.array(image.convert("L"))
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    height, width = gray.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(40, width // 12), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(18, height // 16))
    )
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1
    )
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)
    line_mask = cv2.add(horizontal, vertical)
    line_mask = cv2.dilate(
        line_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1
    )
    rgb[line_mask > 0] = (255, 255, 255)
    return Image.fromarray(rgb)


def _position_to_interval(position: float, lines: tuple[int, ...]) -> int | None:
    if len(lines) < 2 or position < lines[0] or position > lines[-1]:
        return None

    for index, (start, end) in enumerate(zip(lines, lines[1:])):
        if start <= position <= end:
            return index
    return None


def _words_to_cell_text(words: list[dict]) -> str:
    if not words:
        return ""

    sorted_words = sorted(
        words,
        key=lambda word: ((word["bbox"][1] + word["bbox"][3]) / 2, word["bbox"][0]),
    )
    heights = [max(1, word["bbox"][3] - word["bbox"][1]) for word in sorted_words]
    y_tolerance = max(4, int(np.median(heights) * 0.65))

    lines: list[list[dict]] = []
    current_line: list[dict] = []
    current_y: float | None = None

    for word in sorted_words:
        y_center = (word["bbox"][1] + word["bbox"][3]) / 2
        if current_y is None or abs(y_center - current_y) <= y_tolerance:
            current_line.append(word)
            current_y = y_center if current_y is None else (current_y + y_center) / 2
            continue

        lines.append(current_line)
        current_line = [word]
        current_y = y_center

    if current_line:
        lines.append(current_line)

    text_lines = []
    for line in lines:
        ordered = sorted(line, key=lambda word: word["bbox"][0])
        text_lines.append(" ".join(word["text"] for word in ordered))

    return _normalize_cell_text(" ".join(text_lines))


def table_words_to_markdown(table: TableLayout, words: list[dict]) -> str:
    return _rows_to_markdown(table_words_to_rows(table, words))


def table_words_to_rows(table: TableLayout, words: list[dict]) -> list[list[str]]:
    rows: list[list[list[dict]]] = [
        [[] for _ in range(table.cols)] for _ in range(table.rows)
    ]

    for word in words:
        text = _clean_ocr_word(str(word.get("text", "")))
        bbox = word.get("bbox")
        if not text or not bbox or len(bbox) != 4:
            continue

        left, top, right, bottom = bbox
        x_center = (left + right) / 2
        y_center = (top + bottom) / 2
        row = _position_to_interval(y_center, table.y_lines)
        col = _position_to_interval(x_center, table.x_lines)
        if row is None or col is None or row >= table.rows or col >= table.cols:
            continue

        rows[row][col].append(
            {"text": text.replace("|", "\\|"), "bbox": (left, top, right, bottom)}
        )

    return [[_words_to_cell_text(cell_words) for cell_words in row] for row in rows]


def _trim_tiny_edge_columns(table: TableLayout, min_width: int = 18) -> TableLayout:
    x_lines = list(table.x_lines)
    while len(x_lines) > 2 and x_lines[1] - x_lines[0] < min_width:
        x_lines.pop(0)
    while len(x_lines) > 2 and x_lines[-1] - x_lines[-2] < min_width:
        x_lines.pop()

    if tuple(x_lines) == table.x_lines:
        return table

    return TableLayout(
        bbox=table.bbox,
        rows=table.rows,
        cols=len(x_lines) - 1,
        x_lines=tuple(x_lines),
        y_lines=table.y_lines,
        cells=_cells_from_lines(tuple(x_lines), table.y_lines),
    )


def wide_curriculum_table_to_markdown(table: TableLayout, words: list[dict]) -> str:
    table = _trim_tiny_edge_columns(table)
    if table.cols < 45:
        return ""

    rows = table_words_to_rows(table, words)
    data_start = _find_curriculum_data_start(rows)
    if data_start is None:
        return ""

    summary_labels = [
        "Экзамен",
        "Зачет",
        "Зачет с оц.",
        "КП",
        "КР",
        "Экспертное",
        "Факт",
        "Часов в з.е.",
        "Экспертов",
        "По плану",
        "Контакт часов",
        "СР",
        "Контроль",
        "З.е.",
    ]
    semester_labels = [
        "Итого",
        "Лек",
        "Лаб",
        "Пр",
        "КСР",
        "КРП",
        "СР",
        "Контроль",
        "З.е.",
    ]
    semester_start = 16
    semester_count = max(
        0, min(8, (table.cols - semester_start - 3) // len(semester_labels))
    )
    tail_start = semester_start + semester_count * len(semester_labels)

    markdown_rows = [
        [
            "Индекс",
            "Наименование",
            "Контроль/часы",
            "План по семестрам",
            "Кафедра",
            "Компетенции",
        ]
    ]

    for row in rows[data_start:]:
        if not any(cell.strip() for cell in row):
            continue

        row = row + [""] * max(0, table.cols - len(row))
        raw_index = row[0].strip()
        name = row[1].strip() if len(row) > 1 else ""
        index = _normalize_curriculum_index(raw_index, name)
        if not _is_curriculum_index(index):
            if (
                not name
                and _looks_like_text_fragment(raw_index)
                and not _looks_like_wide_header_noise(raw_index)
            ):
                name = raw_index
            index = ""
        else:
            name = _normalize_curriculum_section_name(index, raw_index, name)

        summary = _format_key_values(summary_labels, row[2 : 2 + len(summary_labels)])
        semesters = []
        for semester_index in range(semester_count):
            start = semester_start + semester_index * len(semester_labels)
            values = row[start : start + len(semester_labels)]
            formatted = _format_key_values(semester_labels, values)
            if formatted:
                semesters.append(f"Сем. {semester_index + 1}: {formatted}")

        department = ""
        competencies = ""
        if tail_start < len(row):
            tail = row[tail_start:]
            if len(tail) >= 2:
                department = " ".join(part for part in tail[:-1] if part.strip())
                competencies = tail[-1].strip()
            elif tail:
                competencies = tail[0].strip()

        if (
            not index
            and not name
            and not summary
            and not semesters
            and not department
            and not competencies
        ):
            continue
        if (
            not index
            and not name
            and _looks_like_wide_header_noise(" ".join(row[: min(len(row), 8)]))
        ):
            continue

        markdown_rows.append(
            [
                index,
                name,
                summary,
                "; ".join(semesters),
                department,
                competencies,
            ]
        )

    markdown_rows = _repair_curriculum_index_sequence(markdown_rows)
    return _rows_to_markdown(markdown_rows)


def _find_curriculum_data_start(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows):
        first = row[0].lower() if row else ""
        second = row[1].lower() if len(row) > 1 else ""
        joined = f"{first} {second}"
        if "блок" in joined or "модули" in joined or "обязательн" in joined:
            return index
        normalized = _normalize_curriculum_index(row[0] if row else "", second)
        parsed = _parse_numbered_curriculum_index(normalized)
        if parsed:
            _, number, _ = parsed
            return max(0, index - min(number - 1, 6))
        if _is_curriculum_section_index(normalized):
            return index
    return None


def _format_key_values(labels: list[str], values: list[str]) -> str:
    items = []
    for label, value in zip(labels, values):
        cleaned = value.strip()
        if cleaned:
            items.append(f"{label}: {cleaned}")
    return "; ".join(items)


def _rows_to_markdown(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    rows = [row + [""] * (max_cols - len(row)) for row in rows]

    rows = _normalize_known_table_columns(rows)

    header = [cell or f"Column {index + 1}" for index, cell in enumerate(rows[0])]
    separator = ["---" for _ in header]
    body = rows[1:] or [[" " for _ in header]]
    markdown_rows = [header, separator, *body]

    return "\n".join("| " + " | ".join(row) + " |" for row in markdown_rows)


def _normalize_known_table_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows or not rows[0]:
        return rows

    first_header = rows[0][0].lower()
    if "индекс" not in first_header:
        return rows

    normalized = [rows[0]]
    for row in rows[1:]:
        if not row:
            normalized.append(row)
            continue

        copy = list(row)
        name_cell = copy[1] if len(copy) > 1 else ""
        copy[0] = _normalize_curriculum_index(copy[0], name_cell)
        normalized.append(copy)

    return _fill_missing_curriculum_indexes(normalized)


def _parse_numbered_curriculum_index(text: str) -> tuple[str, int, int] | None:
    match = re.match(r"^(.*\.)(\d+)$", text.strip())
    if not match:
        return None

    prefix, number = match.groups()
    return prefix, int(number), len(number)


def _is_curriculum_section_index(text: str) -> bool:
    value = text.strip()
    if value in {"Б1", "Б1.О", "Б1.В"}:
        return True
    return bool(re.match(r"^Б1\.[ОВ]\.ДВ(?:\.\d+)?$", value))


def _is_curriculum_index(text: str) -> bool:
    value = text.strip()
    return bool(
        value
        and (
            _is_curriculum_section_index(value)
            or _parse_numbered_curriculum_index(value)
        )
    )


def _normalize_curriculum_section_name(index: str, raw_index: str, name: str) -> str:
    if index == "Б1" and "дисциплин" in raw_index.lower():
        section_name = re.sub(
            r"^блок\s*1\.?", "", raw_index, flags=re.IGNORECASE
        ).strip()
        if name and name not in section_name:
            section_name = f"{section_name} {name}"
        return section_name or name
    if (
        index in {"Б1.О", "Б1.В"}
        and raw_index
        and not _parse_numbered_curriculum_index(index)
    ):
        if name and name.lower() not in raw_index.lower():
            return f"{raw_index} {name}"
        return raw_index or name
    return name


def _looks_like_text_fragment(text: str) -> bool:
    value = text.strip()
    if len(value) < 2:
        return False
    if re.search(r"[A-Za-zА-Яа-яЁё]", value) is None:
        return False
    return not bool(re.fullmatch(r"[A-Za-zА-Яа-яЁё]?\d[\d.,ОоOВBв]*", value))


def _looks_like_wide_header_noise(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "индекс",
            "наименование",
            "форма",
            "контрол",
            "экзамен",
            "зачет",
            "зачёт",
            "семестр",
            "итого",
            "недель",
        )
    )


def _row_has_curriculum_payload(row: list[str]) -> bool:
    return any(cell.strip() for cell in row[1:])


def _fill_missing_curriculum_indexes(rows: list[list[str]]) -> list[list[str]]:
    numbered = [
        (index, parsed)
        for index, row in enumerate(rows[1:], start=1)
        if row and (parsed := _parse_numbered_curriculum_index(row[0]))
    ]
    for (left_index, left), (right_index, right) in zip(numbered, numbered[1:]):
        left_prefix, left_number, left_width = left
        right_prefix, right_number, _ = right
        gap = right_index - left_index
        if left_prefix != right_prefix or right_number != left_number + gap:
            continue

        for offset, row_index in enumerate(range(left_index + 1, right_index), start=1):
            row = rows[row_index]
            if not row or row[0].strip() or not _row_has_curriculum_payload(row):
                continue
            row[0] = f"{left_prefix}{left_number + offset:0{left_width}d}"

    return rows


def _repair_curriculum_index_sequence(rows: list[list[str]]) -> list[list[str]]:
    rows = [list(row) for row in rows]
    rows = _fill_missing_curriculum_indexes(rows)

    numbered = [
        (index, parsed)
        for index, row in enumerate(rows[1:], start=1)
        if row and (parsed := _parse_numbered_curriculum_index(row[0]))
    ]
    if not numbered:
        return rows

    first_index, (prefix, number, width) = numbered[0]
    expected = number - 1
    for row_index in range(first_index - 1, 0, -1):
        if expected < 1:
            break
        row = rows[row_index]
        if not row or _is_curriculum_section_index(row[0]):
            break
        if row[0].strip() or not _row_has_curriculum_payload(row):
            continue
        row[0] = f"{prefix}{expected:0{width}d}"
        expected -= 1

    rows = _fill_missing_curriculum_indexes(rows)
    return rows


def _normalize_curriculum_index(text: str, name_cell: str = "") -> str:
    raw = " ".join(text.split())
    compact = raw.replace(" ", "").replace(",", ".")

    lower_name = name_cell.lower()
    combined = f"{raw} {name_cell}".lower()
    if (
        "дисциплин" in combined
        and "модул" in combined
        and (len(compact) <= 24 or "блок" in combined)
    ):
        return "Б1"
    if "обязательн" in combined and (
        not compact
        or "част" in combined
        or compact.lower() in {"si0", "s10", "510", "610", "51o", "61o"}
    ):
        return "Б1.О"
    if (
        "част" in combined
        and "образователь" in combined
        and ("форм" in combined or "фор" in combined or "участ" in combined)
    ):
        return "Б1.В"

    if not compact:
        return raw

    lower_compact = compact.lower()
    if "дисциплин" in lower_name and len(compact) <= 3:
        return "Б1"
    if "обязательная" in lower_name and lower_compact in {
        "si0",
        "s10",
        "510",
        "610",
        "51o",
        "61o",
    }:
        return "Б1.О"

    compact = compact.replace("б", "Б")
    compact = re.sub(r"^[BВ]", "Б", compact)
    match = re.match(r"^(?:[Б56S])*1\.?([0OОoВBв68])\.?(.*)$", compact)
    if not match:
        return raw

    section_raw, rest = match.groups()
    section = "О" if section_raw in {"0", "O", "О", "o"} else "В"
    rest = rest.strip(".")
    if not rest:
        return f"Б1.{section}"

    digit_rest = re.match(r"^(\d+(?:\.\d+)*)", rest)
    if digit_rest:
        rest = digit_rest.group(1)
    if section == "В":
        rest = re.sub(r"^[68ВBв]\.(?=\d+$)", "", rest)
    if rest.isdigit() and len(rest) > 2:
        rest = f"{rest[:-2]}.{rest[-2:]}"

    return _normalize_curriculum_code_ocr_artifacts(f"Б1.{section}.{rest}")


def _normalize_curriculum_code_ocr_artifacts(code: str) -> str:
    if not code.startswith("Б1.В."):
        return code

    code = re.sub(r"^Б1\.В\.Д[д8ВBв]+(?=\.)", "Б1.В.ДВ", code)
    code = re.sub(r"^Б1\.В\.(?:0?48|08|8)(?=\.\d+\.)", "Б1.В.ДВ", code)
    elective_match = re.match(r"^(Б1\.В\.ДВ\.)(\d+)(?:\.(\d+))?(?:\..*)?$", code)
    if elective_match:
        prefix, group_number, option_number = elective_match.groups()
        if option_number is None:
            return f"{prefix}{group_number}"
        return f"{prefix}{group_number}.{option_number[:2]}"
    return code


def _prepare_cell_crop(image: Image.Image, bbox: Box) -> Image.Image:
    left, top, right, bottom = bbox
    inset = max(2, min(8, (right - left) // 20, (bottom - top) // 20))
    crop_box = (
        min(right, left + inset),
        min(bottom, top + inset),
        max(left, right - inset),
        max(top, bottom - inset),
    )
    return image.crop(crop_box)


def _cell_has_visible_content(image: Image.Image) -> bool:
    gray = np.array(image.convert("L"))
    if gray.size == 0:
        return False

    dark = gray < 225
    dark_pixels = int(np.count_nonzero(dark))
    min_ink_pixels = max(5, int(gray.size * 0.0008))
    if dark_pixels < min_ink_pixels:
        return False

    try:
        import cv2

        mask = (dark.astype(np.uint8)) * 255
        component_count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        meaningful_pixels = 0
        for index in range(1, component_count):
            _, _, width, height, area = stats[index]
            if area < 3:
                continue
            if width <= 1 and height <= 1:
                continue
            meaningful_pixels += int(area)
        return meaningful_pixels >= min_ink_pixels
    except Exception:
        return True


def _prepare_cell_for_ocr(image: Image.Image) -> Image.Image:
    gray = ImageOps.autocontrast(image.convert("L"))
    width, height = gray.size
    if width <= 0 or height <= 0:
        return gray

    scale = max(1.0, 52 / max(1, height))
    if width < 120:
        scale = max(scale, 120 / max(1, width))
    scale = min(scale, 4.0)

    if scale > 1.05:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        resized = gray.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            resample,
        )
        gray.close()
        gray = resized

    padded = Image.new("L", (gray.size[0] + 16, gray.size[1] + 16), "white")
    padded.paste(gray, (8, 8))
    gray.close()
    return padded


def table_layout_to_rows(
    image: Image.Image,
    table: TableLayout,
    recognize_cell: Callable[[Image.Image], str],
    *,
    skip_blank_cells: bool = True,
) -> list[list[str]]:
    rows: list[list[str]] = [["" for _ in range(table.cols)] for _ in range(table.rows)]

    for cell in table.cells:
        cell_image = _prepare_cell_crop(image, cell.bbox)
        try:
            if skip_blank_cells and not _cell_has_visible_content(cell_image):
                continue
            prepared = _prepare_cell_for_ocr(cell_image)
            try:
                rows[cell.row][cell.col] = _normalize_cell_text(
                    recognize_cell(prepared)
                )
            finally:
                prepared.close()
        finally:
            cell_image.close()

    return rows


def table_rows_to_markdown(rows: list[list[str]]) -> str:
    return _rows_to_markdown(rows)


def table_layout_to_markdown(
    image: Image.Image,
    table: TableLayout,
    recognize_cell: Callable[[Image.Image], str],
    *,
    skip_blank_cells: bool = True,
) -> str:
    rows = table_layout_to_rows(
        image,
        table,
        recognize_cell,
        skip_blank_cells=skip_blank_cells,
    )

    return _rows_to_markdown(rows)


def find_blank_horizontal_bands(image: Image.Image, min_gap: int = 10) -> list:
    """
    Finds empty horizontal bands (rows with little to no content) between cards.
    Returns list of (start_y, end_y) tuples representing blank band positions.
    """
    width, height = image.size

    # Convert to grayscale array for projection
    gray = image.convert("L")
    img_arr = np.array(gray)

    # Otsu thresholding to separate background and text
    hist, _ = np.histogram(img_arr.flatten(), 256, (0, 256))
    p = hist.astype(float) / img_arr.size
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]

    # Maximize between-class variance
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b_squared = (mu_t * omega - mu) ** 2 / (omega * (1 - omega))

    if np.isnan(sigma_b_squared).all():
        optimal_threshold = 128
    else:
        optimal_threshold = np.nanargmax(sigma_b_squared)

    # Text is dark pixels on white background
    binary = img_arr < optimal_threshold

    # Horizontal projection: calculate total ink (text) per row
    ink = np.sum(binary, axis=1)

    # Consider a row "blank" if very little ink (less than 0.5% of row width)
    blank_rows = ink < (width * 0.005)

    # Find continuous blank gaps
    bands = []
    in_gap = False
    gap_start = 0

    for i, is_blank in enumerate(blank_rows):
        if is_blank and not in_gap:
            in_gap = True
            gap_start = i
        elif not is_blank and in_gap:
            in_gap = False
            gap_height = i - gap_start
            if gap_height >= min_gap:
                bands.append((gap_start, i))

    # Check if image ends with a blank band
    if in_gap:
        gap_height = height - gap_start
        if gap_height >= min_gap:
            bands.append((gap_start, height))

    return bands


def split_by_blank_bands(
    image: Image.Image,
    min_gap: int = 10,
    min_chunk_height: int = 0,
) -> list:
    """
    Splits screenshot by empty horizontal places between cards.
    Returns list of image chunks cropped at blank bands.
    """
    width, height = image.size
    bands = find_blank_horizontal_bands(image, min_gap)

    if not bands:
        return []

    boxes = []
    segment_start = 0

    for start, end in bands:
        if start <= segment_start:
            segment_start = max(segment_start, end)
            continue
        if start - segment_start < min_chunk_height:
            continue
        boxes.append([segment_start, start])
        segment_start = end

    if segment_start < height:
        if boxes and height - segment_start < min_chunk_height:
            boxes[-1][1] = height
        else:
            boxes.append([segment_start, height])

    return [image.crop((0, start, width, end)) for start, end in boxes if end > start]


def fallback_split_with_overlap(
    image: Image.Image, chunk_height: int = 1600, overlap: int = 120
) -> list:
    """
    Fallback: splits image with fixed height and overlap if no blank bands found.
    """
    width, height = image.size
    chunks = []
    y = 0

    while y < height:
        box = (0, y, width, min(y + chunk_height, height))
        chunks.append(image.crop(box))
        y += chunk_height - overlap
        if y >= height - overlap:
            break

    return chunks


def split_vertical(
    image: Image.Image, chunk_height: int = 1600, overlap: int = 120
) -> list:
    """
    Main entry point: splits image vertically using card-aware chunking.
    First tries to find blank horizontal bands between cards.
    Falls back to fixed sizing with overlap if no blank bands found.
    """
    try:
        image = remove_white_borders(image)
        _, height = image.size
        if height <= chunk_height:
            return [image]

        # Try card-aware splitting first
        chunks = split_by_blank_bands(
            image,
            min_chunk_height=max(1, chunk_height // 4),
        )

        if len(chunks) > 1:
            return chunks

        # Fallback to overlap-based splitting
        return fallback_split_with_overlap(image, chunk_height, overlap)

    except Exception as e:
        print(f"Error in split_vertical: {e}")
        # Fallback simple overlap
        return fallback_split_with_overlap(image, chunk_height, overlap)
