from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

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


def _line_positions(mask: np.ndarray, *, axis: int, min_coverage: int) -> list[int]:
    projection = np.count_nonzero(mask > 0, axis=axis)
    indexes = np.where(projection >= min_coverage)[0]
    return [int(round((start + end) / 2)) for start, end in _group_indexes(indexes)]


def _with_edges(positions: list[int], size: int, tolerance: int = 8) -> tuple[int, ...]:
    if size <= 1:
        return tuple(sorted(set(positions)))

    result = list(positions)
    if not any(pos <= tolerance for pos in result):
        result.append(0)
    if not any(pos >= size - 1 - tolerance for pos in result):
        result.append(size - 1)
    return tuple(sorted(set(max(0, min(size - 1, pos)) for pos in result)))


def _cells_from_lines(x_lines: tuple[int, ...], y_lines: tuple[int, ...]) -> tuple[TableCell, ...]:
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
    for table in sorted(tables, key=lambda t: (-(t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]), t.bbox[1])):
        if any(_intersection_over_union(table.bbox, existing.bbox) > 0.65 for existing in selected):
            continue
        selected.append(table)
    return sorted(selected, key=lambda t: (t.bbox[1], t.bbox[0]))


def detect_table_layouts(image: Image.Image, min_cells: int = 4) -> list[TableLayout]:
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

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(24, width // 22), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(24, height // 22)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)
    grid = cv2.add(horizontal, vertical)
    grid = cv2.dilate(grid, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)

    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    image_area = width * height
    tables: list[TableLayout] = []

    for contour in contours:
        x, y, table_width, table_height = cv2.boundingRect(contour)
        table_area = table_width * table_height
        if table_width < 100 or table_height < 70:
            continue
        if table_area < max(6_000, int(image_area * 0.015)):
            continue

        x2 = min(width, x + table_width)
        y2 = min(height, y + table_height)
        table_horizontal = horizontal[y:y2, x:x2]
        table_vertical = vertical[y:y2, x:x2]
        local_width = x2 - x
        local_height = y2 - y

        y_lines_local = _with_edges(
            _line_positions(table_horizontal, axis=1, min_coverage=max(25, int(local_width * 0.35))),
            local_height,
        )
        x_lines_local = _with_edges(
            _line_positions(table_vertical, axis=0, min_coverage=max(20, int(local_height * 0.35))),
            local_width,
        )

        if len(x_lines_local) < 3 or len(y_lines_local) < 3:
            continue

        x_lines = tuple(x + pos for pos in x_lines_local)
        y_lines = tuple(y + pos for pos in y_lines_local)
        cells = _cells_from_lines(x_lines, y_lines)
        rows = len(y_lines) - 1
        cols = len(x_lines) - 1
        if rows * cols < min_cells or len(cells) < min_cells:
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
                bbox=(cell.bbox[0] + dx, cell.bbox[1] + dy, cell.bbox[2] + dx, cell.bbox[3] + dy),
            )
            for cell in table.cells
        ),
    )


def analyze_document_layout(image: Image.Image) -> list[LayoutRegion]:
    """
    Split a page into ordered text/image and table regions.

    Table regions are returned as isolated crops with a table layout relative to
    that crop. If no tables are found, callers can keep using legacy chunking.
    """
    cropped = remove_white_borders(image)
    width, height = cropped.size
    tables = detect_table_layouts(cropped)
    if not tables:
        return [LayoutRegion(kind="image", image=cropped, bbox=(0, 0, width, height))]

    regions: list[LayoutRegion] = []
    cursor_y = 0

    for table in tables:
        x1, y1, x2, y2 = table.bbox
        if y1 - cursor_y > 40:
            text_crop = cropped.crop((0, cursor_y, width, y1))
            if _has_visible_content(text_crop):
                regions.append(LayoutRegion(kind="image", image=text_crop, bbox=(0, cursor_y, width, y1)))

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
            regions.append(LayoutRegion(kind="image", image=text_crop, bbox=(0, cursor_y, width, height)))

    return regions


def _normalize_cell_text(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def table_layout_to_markdown(
    image: Image.Image,
    table: TableLayout,
    recognize_cell: Callable[[Image.Image], str],
) -> str:
    rows: list[list[str]] = [["" for _ in range(table.cols)] for _ in range(table.rows)]

    for cell in table.cells:
        left, top, right, bottom = cell.bbox
        inset = max(2, min(6, (right - left) // 25, (bottom - top) // 25))
        crop_box = (
            min(right, left + inset),
            min(bottom, top + inset),
            max(left, right - inset),
            max(top, bottom - inset),
        )
        cell_image = image.crop(crop_box)
        rows[cell.row][cell.col] = _normalize_cell_text(recognize_cell(cell_image))

    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""

    non_empty_columns = [
        col_index
        for col_index in range(table.cols)
        if any(col_index < len(row) and row[col_index].strip() for row in rows)
    ]
    if non_empty_columns:
        rows = [[row[col_index] for col_index in non_empty_columns] for row in rows]

    header = [cell or f"Column {index + 1}" for index, cell in enumerate(rows[0])]
    separator = ["---" for _ in header]
    body = rows[1:] or [[" " for _ in header]]
    markdown_rows = [header, separator, *body]

    return "\n".join("| " + " | ".join(row) + " |" for row in markdown_rows)


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


def split_by_blank_bands(image: Image.Image, min_gap: int = 10) -> list:
    """
    Splits screenshot by empty horizontal places between cards.
    Returns list of image chunks cropped at blank bands.
    """
    width, height = image.size
    bands = find_blank_horizontal_bands(image, min_gap)

    if not bands:
        return []

    chunks = []
    last_end = 0

    for start, end in bands:
        # Cut from last_end to start of blank band
        if start > last_end:
            chunks.append(image.crop((0, last_end, width, start)))
        last_end = end

    # Add remaining part after last blank band
    if last_end < height:
        chunks.append(image.crop((0, last_end, width, height)))

    return chunks


def fallback_split_with_overlap(image: Image.Image, chunk_height: int = 1600, overlap: int = 120) -> list:
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


def split_vertical(image: Image.Image, chunk_height: int = 1600, overlap: int = 120) -> list:
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
        chunks = split_by_blank_bands(image)

        # If we found meaningful chunks (more than 1, and each is reasonable size)
        if len(chunks) > 1:
            # Filter out very small chunks (likely noise)
            min_chunk_height = chunk_height // 4
            filtered_chunks = [c for c in chunks if c.size[1] >= min_chunk_height]
            if filtered_chunks:
                return filtered_chunks

        # Fallback to overlap-based splitting
        return fallback_split_with_overlap(image, chunk_height, overlap)

    except Exception as e:
        print(f"Error in split_vertical: {e}")
        # Fallback simple overlap
        return fallback_split_with_overlap(image, chunk_height, overlap)
