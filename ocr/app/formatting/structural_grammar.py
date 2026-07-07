from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.layout.sparse_codes import (
    MERGE_BOTH_CODES,
    MERGE_LEFT_CODES,
    MERGE_UP_CODES,
    MERGE_UP_ONLY_CODES,
)

DERIVED_TABLE_CODE = 7


@dataclass(frozen=True)
class SparseMarkdownRow:
    parts: Iterable[str]
    anchor: tuple[int, int]
    codes: tuple[tuple[int, int, int], ...]
    list_marker: bool = False
    content_left: int | None = None


@dataclass(frozen=True)
class StructuralMarkdownResult:
    markdown: str
    lint_errors: tuple[str, ...]


@dataclass(frozen=True)
class _TableIsland:
    rows: tuple[SparseMarkdownRow, ...]
    width: int


def render_sparse_markdown_rows(
    rows: list[SparseMarkdownRow],
    *,
    first_heading_level: int = 1,
) -> StructuralMarkdownResult:
    groups = _shadow_components(rows)
    rendered: list[tuple[str, str]] = []
    previous_rendered_group_rows: list[SparseMarkdownRow] = []
    first_content_group = first_heading_level == 1
    first_heading_marker = "#" * max(1, min(6, first_heading_level))
    index = 0
    while index < len(groups):
        repeating_grid = _repeating_vertical_grid_at(groups, index)
        if repeating_grid:
            list_groups, table_groups = repeating_grid
            for group_rows, _, _, _ in list_groups:
                lines = [line for row in group_rows for line in _content_lines(row.parts)]
                if lines:
                    rendered.append(
                        (
                            "list",
                            "- " + _strip_list_marker(" ".join(lines)),
                        )
                    )
            rendered.append(
                (
                    "table",
                    _repeating_vertical_grid_table(table_groups),
                )
            )
            first_content_group = False
            previous_rendered_group_rows = table_groups[-1][0]
            index += len(list_groups) + len(table_groups)
            continue

        table_band = _table_band_at(groups, index)
        if table_band:
            rendered.append(
                (
                    "table",
                    _sparse_table_band(table_band),
                )
            )
            first_content_group = False
            previous_rendered_group_rows = table_band[-1][0]
            index += len(table_band)
            continue

        (
            group_rows,
            merge_up_count,
            merge_left_count,
            dash_count,
        ) = groups[index]
        lines_by_row = [_content_lines(tuple(row.parts)) for row in group_rows]
        lines = [line for row_lines in lines_by_row for line in row_lines]
        if not lines:
            index += 1
            continue
        list_like = _is_list_like_component(
            group_rows,
            lines_by_row,
            merge_left_count=merge_left_count,
            first_content_group=first_content_group,
            previous_group_rows=previous_rendered_group_rows,
            previous_rendered_kind=(rendered[-1][0] if rendered else ""),
        )

        if implicit_list := _implicit_indented_list_render(
            group_rows,
            lines_by_row,
            merge_left_count=merge_left_count,
            first_content_group=first_content_group,
            previous_group_rows=previous_rendered_group_rows,
        ):
            rendered.extend(implicit_list)
            first_content_group = False
            previous_rendered_group_rows = group_rows
            index += 1
            continue
        if list_like:
            value = "- " + _strip_list_marker(" ".join(lines))
            kind = "list"
        elif _is_table_like_component(
            group_rows,
            merge_left_count=merge_left_count,
            first_content_group=first_content_group,
            allow_first_table=first_heading_level == 1,
        ):
            value = _sparse_table_row(group_rows, lines)
            kind = "table"
        elif first_content_group and merge_up_count <= 2:
            value = f"{first_heading_marker} " + " ".join(lines)
            kind = "heading"
        elif first_content_group:
            tail_rows = [" ".join(row_lines).strip() for row_lines in lines_by_row[1:] if row_lines]
            tail_heading_count = _first_component_tail_heading_count(
                groups,
                index,
                tail_rows,
            )
            rendered.append(
                (
                    "heading",
                    f"{first_heading_marker} " + " ".join(lines_by_row[0]).strip(),
                )
            )
            paragraph_tail = tail_rows[:-tail_heading_count] if tail_heading_count else tail_rows
            heading_tail = tail_rows[-tail_heading_count:] if tail_heading_count else []
            if paragraph_tail:
                rendered.append(("paragraph", " ".join(paragraph_tail).strip()))
            for heading in heading_tail:
                rendered.append(("heading", "## " + heading))
            first_content_group = False
            previous_rendered_group_rows = group_rows
            index += 1
            continue
        elif indented_tail := _indented_tail_render(
            group_rows,
            lines_by_row,
            merge_left_count=merge_left_count,
            previous_rendered_kind=rendered[-1][0] if rendered else "",
        ):
            rendered.extend(indented_tail)
            first_content_group = False
            previous_rendered_group_rows = group_rows
            index += 1
            continue
        elif section_tail := _section_heading_with_tail(
            groups,
            index,
            merge_left_count=merge_left_count,
            lines_by_row=lines_by_row,
        ):
            rendered.extend(section_tail)
            first_content_group = False
            previous_rendered_group_rows = group_rows
            index += 1
            continue
        elif _has_heading_prefix_before_list(
            groups,
            index,
            merge_left_count=merge_left_count,
        ):
            rendered.append(
                (
                    "heading",
                    "## " + " ".join(lines_by_row[0]).strip(),
                )
            )
            tail = [" ".join(row_lines).strip() for row_lines in lines_by_row[1:] if row_lines]
            if tail:
                rendered.append(("paragraph", " ".join(tail)))
            first_content_group = False
            previous_rendered_group_rows = group_rows
            index += 1
            continue
        elif merge_left_count > 0:
            value = " ".join(lines)
            kind = "paragraph"
        elif _is_section_heading_candidate(
            groups,
            index,
            merge_up_count=merge_up_count,
            dash_count=1 if list_like else 0,
            previous_rendered_kind=rendered[-1][0] if rendered else "",
            previous_heading_level=_heading_level(rendered[-1][1]) if rendered else 0,
        ):
            value = "## " + " ".join(lines)
            kind = "heading"
        else:
            value = " ".join(lines)
            kind = "paragraph"
        rendered.append((kind, value.strip()))
        first_content_group = False
        previous_rendered_group_rows = group_rows
        index += 1

    chunks = []
    previous_kind = ""
    for kind, value in rendered:
        separator = "\n" if kind == previous_kind and kind in {"list", "table"} else "\n\n"
        if chunks:
            chunks.append(separator)
        chunks.append(value)
        previous_kind = kind
    markdown = "".join(chunks).strip()
    return StructuralMarkdownResult(
        markdown=markdown,
        lint_errors=lint_markdown_structure(markdown),
    )


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized_rows = [[*row, *([""] * (width - len(row)))] for row in rows]
    header = "| " + " | ".join(normalized_rows[0]) + " |"
    separator = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in normalized_rows[1:]]
    return "\n".join((header, separator, *body))


def _sparse_table_row(
    group_rows: list[SparseMarkdownRow],
    lines: list[str],
) -> str:
    islands = _component_table_islands(group_rows)
    if islands:
        tables = []
        for island in islands:
            table_rows = []
            for row in island.rows:
                row_lines = _content_lines(row.parts)
                cells = [""] * island.width
                cells[0] = " ".join(row_lines).strip()
                table_rows.append(cells)
            tables.append(_markdown_table(table_rows))
        return "\n\n".join(tables)

    logical_rows = _component_logical_table_rows(group_rows)
    if len(logical_rows) >= 3:
        width = max(
            2,
            max(_row_table_width(row) for row in logical_rows),
        )
        table_rows = []
        for row in logical_rows:
            row_lines = _content_lines(row.parts)
            cells = [""] * width
            cells[0] = " ".join(row_lines).strip()
            table_rows.append(cells)
        return _markdown_table(table_rows)

    if _component_matrix_table_signal(group_rows):
        tables = [
            _markdown_table(table_rows)
            for columns in _component_column_segments(group_rows)
            if (
                table_rows := _component_matrix_table_rows(
                    group_rows,
                    columns,
                )
            )
        ]
        if tables:
            return "\n\n".join(tables)

    width = _component_table_width(group_rows)
    cells = [""] * width
    cells[0] = " ".join(lines).strip()
    return _markdown_table([cells])


def _component_matrix_table_rows(
    group_rows: list[SparseMarkdownRow],
    columns: tuple[int, ...],
) -> list[list[str]]:
    if len(columns) < 2:
        return []
    column_index = {column: index for index, column in enumerate(columns)}
    rows_by_number: dict[int, list[str]] = {}
    for row in sorted(group_rows, key=lambda value: value.anchor):
        row_lines = _content_lines(row.parts)
        if not row_lines:
            continue
        if row.anchor[1] not in column_index:
            continue
        cells = rows_by_number.setdefault(
            row.anchor[0],
            [""] * len(columns),
        )
        index = column_index[row.anchor[1]]
        text = " ".join(row_lines).strip()
        cells[index] = f"{cells[index]} {text}".strip() if cells[index] else text
    return [
        rows_by_number[row_number]
        for row_number in sorted(rows_by_number)
        if any(cell.strip() for cell in rows_by_number[row_number])
    ]


def _component_column_segments(
    group_rows: list[SparseMarkdownRow],
) -> list[tuple[int, ...]]:
    occupied_columns = _component_columns(group_rows)
    if len(occupied_columns) < 2:
        return []
    all_columns = tuple(
        range(
            min(occupied_columns),
            max(occupied_columns) + 1,
        )
    )
    boundary_columns = _matrix_boundary_columns(group_rows)
    if not boundary_columns:
        return [all_columns]

    segments: list[list[int]] = [[]]
    for column in all_columns:
        if column in boundary_columns:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(column)
    return [tuple(segment) for segment in segments if len(segment) >= 2] or [all_columns]


def _matrix_boundary_columns(
    group_rows: list[SparseMarkdownRow],
) -> set[int]:
    anchors = {row.anchor[1] for row in group_rows}
    up_only_rows_by_column: dict[int, set[int]] = {}
    for row in group_rows:
        for row_number, column, code in row.codes:
            if code in MERGE_UP_ONLY_CODES:
                up_only_rows_by_column.setdefault(column, set()).add(row_number)

    candidates = {column for column, rows in up_only_rows_by_column.items() if len(rows) >= 2 and column not in anchors}
    boundary_columns: set[int] = set()
    run: list[int] = []
    for column in sorted(candidates):
        if not run or column == run[-1] + 1:
            run.append(column)
        else:
            if len(run) >= 2:
                boundary_columns.update(run)
            run = [column]
    if len(run) >= 2:
        boundary_columns.update(run)
    boundary_columns.update(column for column in _wide_row_gap_boundary_columns(group_rows) if column not in anchors)
    return boundary_columns


def _wide_row_gap_boundary_columns(
    group_rows: list[SparseMarkdownRow],
) -> set[int]:
    boundaries: set[int] = set()
    for row in group_rows:
        merge_columns = sorted(
            {
                column
                for row_number, column, code in row.codes
                if (row_number == row.anchor[0] and code in MERGE_LEFT_CODES)
            }
        )
        if len(merge_columns) < 6:
            continue
        runs = [run for run in _consecutive_column_runs(merge_columns) if len(run) >= 2]
        if len(runs) < 2:
            continue
        for first, second in zip(runs, runs[1:]):
            boundaries.update(range(first[-1] + 1, second[0]))
    return boundaries


def _consecutive_column_runs(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    runs: list[list[int]] = [[values[0]]]
    for value in values[1:]:
        if value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def _component_table_islands(
    group_rows: list[SparseMarkdownRow],
) -> list[_TableIsland]:
    accepted_single_runs = _single_left_table_runs(group_rows)
    blocked_ranges = [(run[0].anchor[0], run[-1].anchor[0]) for run in accepted_single_runs]
    strong_islands = _strong_table_islands(
        group_rows,
        blocked_ranges=blocked_ranges,
    )
    islands: list[_TableIsland] = [
        _TableIsland(
            rows=tuple(rows),
            width=max(2, max(_row_table_width(row) for row in rows)),
        )
        for rows in strong_islands
    ]

    strong_rows = [row for row in group_rows if _row_merge_left_count(row) >= 2]
    for run in accepted_single_runs:
        schema = _nearest_schema_row(strong_rows, run)
        rows = tuple((*((schema,) if schema is not None else ()), *run))
        width = max(
            2,
            *(_row_table_width(row) for row in rows),
            _row_table_width(schema) if schema is not None else 0,
        )
        islands.append(_TableIsland(rows=rows, width=width))

    islands = sorted(islands, key=lambda island: island.rows[0].anchor)
    deduped: list[_TableIsland] = []
    seen: set[tuple[tuple[int, int], ...]] = set()
    for island in islands:
        key = tuple(row.anchor for row in island.rows)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(island)
    return deduped


def _strong_table_islands(
    group_rows: list[SparseMarkdownRow],
    *,
    blocked_ranges: list[tuple[int, int]],
) -> list[list[SparseMarkdownRow]]:
    strong_rows = [row for row in sorted(group_rows, key=lambda row: row.anchor) if _row_merge_left_count(row) >= 2]
    if len(strong_rows) < 3:
        return []

    islands: list[list[SparseMarkdownRow]] = [[strong_rows[0]]]
    for previous, row in zip(strong_rows, strong_rows[1:]):
        crosses_single_run = any(previous.anchor[0] < start <= end < row.anchor[0] for start, end in blocked_ranges)
        if row.anchor[0] - previous.anchor[0] > 10 or crosses_single_run:
            islands.append([row])
        else:
            islands[-1].append(row)

    result = []
    for island in islands:
        rows = [
            *island,
            *_bridging_single_left_rows(group_rows, island),
        ]
        rows = sorted(rows, key=lambda row: row.anchor)
        if len(rows) >= 3:
            result.append(rows)
    return result


def _single_left_table_runs(
    group_rows: list[SparseMarkdownRow],
) -> list[tuple[SparseMarkdownRow, ...]]:
    candidates = [row for row in sorted(group_rows, key=lambda row: row.anchor) if _row_merge_left_count(row) == 1]
    if not candidates:
        return []

    runs: list[list[SparseMarkdownRow]] = [[candidates[0]]]
    for row in candidates[1:]:
        previous = runs[-1][-1]
        if row.anchor[0] - previous.anchor[0] <= 4 and _single_left_run_compatible(runs[-1], row):
            runs[-1].append(row)
        else:
            runs.append([row])

    return [tuple(run) for run in runs if len(run) >= 4]


def _single_left_run_compatible(
    run: list[SparseMarkdownRow],
    candidate: SparseMarkdownRow,
) -> bool:
    candidate_columns = set(_row_merge_left_columns(candidate))
    if not candidate_columns:
        return False
    if len(run) < 2:
        return True

    counts: dict[int, int] = {}
    for row in run:
        for column in _row_merge_left_columns(row):
            counts[column] = counts.get(column, 0) + 1
    maximum = max(counts.values(), default=0)
    dominant = {column for column, count in counts.items() if count == maximum and count >= 2}
    if not dominant:
        return True
    return bool(candidate_columns & dominant)


def _nearest_schema_row(
    strong_rows: list[SparseMarkdownRow],
    run: tuple[SparseMarkdownRow, ...],
) -> SparseMarkdownRow | None:
    if len(run) < 6:
        return None
    first_row = run[0].anchor[0]
    candidates = [row for row in strong_rows if 0 < first_row - row.anchor[0] <= 2]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.anchor[0])


def _sparse_table_band(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
) -> str:
    width = _band_table_width(groups)
    rows = []
    for group_rows, _, _, _ in groups:
        lines = [line for row in group_rows for line in _content_lines(row.parts)]
        cells = [""] * width
        cells[0] = " ".join(lines).strip()
        rows.append(cells)
    return _markdown_table(rows)


def _repeating_vertical_grid_at(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    start: int,
) -> (
    tuple[
        list[tuple[list[SparseMarkdownRow], int, int, int]],
        list[tuple[list[SparseMarkdownRow], int, int, int]],
    ]
    | None
):
    list_groups = []
    index = start
    while index < len(groups) and _is_leading_grid_chip(groups[index]):
        list_groups.append(groups[index])
        index += 1
    if len(list_groups) < 2 or index >= len(groups):
        return None

    table_groups = groups[index:]
    if len(table_groups) < 4:
        return None
    repeated = [group for group in table_groups if _is_repeated_vertical_grid_component(group)]
    if len(repeated) < 4:
        return None

    anchor_columns: dict[int, int] = {}
    spans: dict[int, int] = {}
    for group_rows, _, _, _ in repeated:
        anchor = min(row.anchor[1] for row in group_rows)
        span = _component_column_span(group_rows)
        anchor_columns[anchor] = anchor_columns.get(anchor, 0) + 1
        spans[span] = spans.get(span, 0) + 1

    dominant_anchor = max(anchor_columns.values(), default=0)
    dominant_span = max(spans.values(), default=0)
    if dominant_anchor < 3 and dominant_span < 3:
        return None
    return list_groups, table_groups


def _is_leading_grid_chip(
    group: tuple[list[SparseMarkdownRow], int, int, int],
) -> bool:
    group_rows, merge_up_count, merge_left_count, _ = group
    return (
        len(group_rows) == 1
        and merge_up_count == 0
        and merge_left_count == 1
        and _component_column_span(group_rows) >= 6
    )


def _is_repeated_vertical_grid_component(
    group: tuple[list[SparseMarkdownRow], int, int, int],
) -> bool:
    group_rows, merge_up_count, merge_left_count, _ = group
    top, bottom = _component_row_bounds(group_rows)
    return (
        len(group_rows) >= 4
        and bottom - top >= 3
        and merge_up_count >= len(group_rows) - 2
        and merge_left_count >= 3
        and _component_column_span(group_rows) >= 6
    )


def _component_column_span(group_rows: list[SparseMarkdownRow]) -> int:
    columns = _component_columns(group_rows)
    if not columns:
        return 0
    return max(columns) - min(columns) + 1


def _repeating_vertical_grid_table(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
) -> str:
    width = max(
        2,
        max(_component_column_span(group_rows) for group_rows, _, _, _ in groups),
    )
    rows = []
    for group_rows, _, _, _ in groups:
        lines = [line for row in group_rows for line in _content_lines(row.parts)]
        if not lines:
            continue
        cells = [""] * width
        cells[0] = " ".join(lines).strip()
        rows.append(cells)
    return _markdown_table(rows)


def _component_table_width(group_rows: list[SparseMarkdownRow]) -> int:
    if _component_matrix_table_signal(group_rows):
        return max(2, _component_width(group_rows))
    return 2


def _band_table_width(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
) -> int:
    return max(
        2,
        max(_component_width(group_rows) for group_rows, _, _, _ in groups),
    )


def _component_logical_table_rows(
    group_rows: list[SparseMarkdownRow],
) -> list[SparseMarkdownRow]:
    rows = [row for row in group_rows if _row_merge_left_count(row) >= 2]
    rows.extend(_bridging_single_left_rows(group_rows, rows))
    rows = sorted(rows, key=lambda row: row.anchor)
    if len(rows) < 3:
        return []
    if max(_row_table_width(row) for row in rows) < 3:
        return []
    return rows


def _bridging_single_left_rows(
    group_rows: list[SparseMarkdownRow],
    strong_rows: list[SparseMarkdownRow],
) -> list[SparseMarkdownRow]:
    if len(strong_rows) < 2:
        return []
    strong_numbers = sorted(row.anchor[0] for row in strong_rows)
    result = []
    for first, second in zip(strong_numbers, strong_numbers[1:]):
        candidates = [row for row in group_rows if first < row.anchor[0] < second and _row_merge_left_count(row) == 1]
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        if candidate.anchor[0] - first >= 3 and second - candidate.anchor[0] >= 3:
            result.append(candidate)
    return result


def _row_merge_left_count(row: SparseMarkdownRow) -> int:
    return sum(1 for _, _, code in row.codes if code in MERGE_LEFT_CODES)


def _row_merge_left_columns(row: SparseMarkdownRow) -> tuple[int, ...]:
    return tuple(column for _, column, code in row.codes if code in MERGE_LEFT_CODES)


def _row_table_width(row: SparseMarkdownRow) -> int:
    return max(
        1,
        len(
            {
                row.anchor[1],
                *(column for row_number, column, _ in row.codes if row_number == row.anchor[0]),
            }
        ),
    )


def _component_columns(
    group_rows: list[SparseMarkdownRow],
) -> tuple[int, ...]:
    return tuple(
        sorted({row.anchor[1] for row in group_rows} | {column for row in group_rows for _, column, _ in row.codes})
    )


def _component_width(group_rows: list[SparseMarkdownRow]) -> int:
    return len(_component_columns(group_rows))


def _component_row_bounds(
    group_rows: list[SparseMarkdownRow],
) -> tuple[int, int]:
    rows = [row.anchor[0] for row in group_rows]
    return min(rows), max(rows)


def _table_band_at(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    start: int,
) -> list[tuple[list[SparseMarkdownRow], int, int, int]]:
    group_rows, _, merge_left_count, _ = groups[start]
    if merge_left_count <= 0 or _component_width(group_rows) < 2:
        return []

    band = [groups[start]]
    band_columns = set(_component_columns(group_rows))
    _, previous_bottom = _component_row_bounds(group_rows)
    for group in groups[start + 1 :]:
        next_rows, _, next_left_count, _ = group
        next_top, next_bottom = _component_row_bounds(next_rows)
        if next_top - previous_bottom > 1:
            break
        next_columns = set(_component_columns(next_rows))
        if not (next_columns & band_columns) and (
            min(next_columns, default=0) > max(band_columns, default=0) + 1
            or min(band_columns, default=0) > max(next_columns, default=0) + 1
        ):
            break
        starts_new_section = next_left_count == 0 and min(next_columns, default=0) == 0
        continues_band = next_left_count > 0 or (bool(next_columns & band_columns) and min(next_columns, default=0) > 0)
        if starts_new_section or not continues_band:
            break
        band.append(group)
        band_columns.update(next_columns)
        previous_bottom = next_bottom

    if len(band) < 2:
        return []
    if max(_component_width(rows) for rows, _, _, _ in band) < 3:
        return []
    if not _band_matrix_table_signal(band):
        return []
    return band


def _band_matrix_table_signal(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
) -> bool:
    all_rows = [row for group_rows, _, _, _ in groups for row in group_rows]
    if _component_matrix_table_signal(all_rows):
        return True

    left_components = sum(1 for _, _, merge_left_count, _ in groups if merge_left_count > 0)
    widest_component = max(_component_width(group_rows) for group_rows, _, _, _ in groups)
    if left_components >= 4 and widest_component >= 3:
        return True

    left_columns = {
        column
        for group_rows, _, _, _ in groups
        for row in group_rows
        for _, column, code in row.codes
        if code in MERGE_LEFT_CODES
    }
    return len(groups) >= 3 and _longest_consecutive_run(left_columns) >= 4


def _component_matrix_table_signal(
    group_rows: list[SparseMarkdownRow],
) -> bool:
    # DERIVED_TABLE_CODE is intentionally not written back into the raw
    # sparse shadow. It is the grammar layer's "this behaves like a table"
    # signal after reading the 3/5/8 matrix.
    return _has_row_table_run(group_rows) or _has_merge_both_rectangle(group_rows)


def _has_row_table_run(
    group_rows: list[SparseMarkdownRow],
) -> bool:
    cells_by_row: dict[int, set[int]] = {}
    for row in group_rows:
        cells_by_row.setdefault(row.anchor[0], set()).add(row.anchor[1])
        for row_number, column, _ in row.codes:
            cells_by_row.setdefault(row_number, set()).add(column)
    return any(_longest_consecutive_run(columns) >= 4 for columns in cells_by_row.values())


def _has_merge_both_rectangle(
    group_rows: list[SparseMarkdownRow],
) -> bool:
    eight_columns_by_row: dict[int, set[int]] = {}
    for row in group_rows:
        for row_number, column, code in row.codes:
            if code in MERGE_BOTH_CODES:
                eight_columns_by_row.setdefault(row_number, set()).add(column)

    row_numbers = sorted(eight_columns_by_row)
    for first_index, first_row in enumerate(row_numbers):
        for second_row in row_numbers[first_index + 1 :]:
            common_columns = eight_columns_by_row[first_row] & eight_columns_by_row[second_row]
            if _longest_consecutive_run(common_columns) >= 2:
                return True
    return False


def _longest_consecutive_run(values: set[int] | tuple[int, ...]) -> int:
    longest = 0
    current = 0
    previous: int | None = None
    for value in sorted(set(values)):
        if previous is None or value == previous + 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = value
    return longest


def _is_table_like_component(
    group_rows: list[SparseMarkdownRow],
    *,
    merge_left_count: int,
    first_content_group: bool,
    allow_first_table: bool,
) -> bool:
    del group_rows, first_content_group, allow_first_table
    return merge_left_count > 0


def _is_list_like_component(
    group_rows: list[SparseMarkdownRow],
    lines_by_row: list[list[str]],
    *,
    merge_left_count: int,
    first_content_group: bool,
    previous_group_rows: list[SparseMarkdownRow],
    previous_rendered_kind: str,
) -> bool:
    if first_content_group or merge_left_count > 0:
        return False
    if any(lines and re.match(r"^(?:[-*+]|\d+[.)])\s+\S", lines[0]) for lines in lines_by_row):
        return True
    if any(row.list_marker for row in group_rows):
        return True
    current_column = min(
        (row.anchor[1] for row in group_rows),
        default=0,
    )
    previous_column = min(
        (row.anchor[1] for row in previous_group_rows),
        default=current_column,
    )
    if previous_rendered_kind == "list":
        return current_column >= previous_column
    return previous_rendered_kind == "paragraph" and previous_column == 1 and current_column == 3


def _first_component_tail_heading_count(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    index: int,
    tail_rows: list[str],
) -> int:
    if len(tail_rows) < 2:
        return 0
    if not _structured_group_ahead(groups, index, lookahead=1):
        return 0
    return min(2, len(tail_rows))


def _is_section_heading_candidate(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    index: int,
    *,
    merge_up_count: int,
    dash_count: int,
    previous_rendered_kind: str,
    previous_heading_level: int,
) -> bool:
    del previous_rendered_kind, previous_heading_level
    if dash_count > 0:
        return False
    group_rows = groups[index][0]
    if len(group_rows) != 1 or merge_up_count != 0 or min((row.anchor[1] for row in group_rows), default=1) != 0:
        return False
    text = " ".join(line for row in group_rows for line in _content_lines(tuple(row.parts))).strip()
    if text.endswith((".", "!", "?", ";", ":")):
        return False
    previous_row = max(row.anchor[0] for row in groups[index - 1][0]) if index > 0 and groups[index - 1][0] else None
    current_row = min(row.anchor[0] for row in group_rows)
    next_dash_or_table = False
    if index + 1 < len(groups):
        _, _, next_left_count, next_dash_count = groups[index + 1]
        next_dash_or_table = next_left_count > 0 or next_dash_count > 0
    return previous_row is None or current_row - previous_row > 1 or next_dash_or_table


def _section_heading_with_tail(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    index: int,
    *,
    merge_left_count: int,
    lines_by_row: list[list[str]],
) -> list[tuple[str, str]]:
    group_rows = groups[index][0]
    if (
        index == 0
        or merge_left_count > 0
        or len(group_rows) < 3
        or min((row.anchor[1] for row in group_rows), default=1) != 0
    ):
        return []
    previous_rows = groups[index - 1][0]
    if not previous_rows:
        return []
    previous_bottom = max(row.anchor[0] for row in previous_rows)
    current_top = min(row.anchor[0] for row in group_rows)
    if current_top - previous_bottom <= 1:
        return []
    if any(code in MERGE_UP_CODES for _, _, code in group_rows[0].codes):
        return []
    heading = " ".join(lines_by_row[0]).strip()
    if not heading or heading.endswith((".", "!", "?", ";")):
        return []
    tail = [" ".join(lines).strip() for lines in lines_by_row[1:] if lines]
    if not tail:
        return []
    return [
        ("heading", "## " + heading),
        ("paragraph", " ".join(tail)),
    ]


def _indented_tail_render(
    group_rows: list[SparseMarkdownRow],
    lines_by_row: list[list[str]],
    *,
    merge_left_count: int,
    previous_rendered_kind: str,
) -> list[tuple[str, str]]:
    if previous_rendered_kind != "list" or merge_left_count > 0 or len(group_rows) < 5:
        return []
    lefts = [row.content_left for row in group_rows]
    if any(left is None for left in lefts):
        return []
    numeric_lefts = [int(left) for left in lefts if left is not None]
    baseline = min(numeric_lefts)
    indent_threshold = baseline + 12
    first_indented = next(
        (index for index, left in enumerate(numeric_lefts) if left >= indent_threshold),
        None,
    )
    if first_indented is None or first_indented < 2:
        return []
    if len(group_rows) - first_indented < 3:
        return []

    heading = " ".join(lines_by_row[0]).strip()
    if not heading or heading.endswith((".", "!", "?", ";")):
        return []

    result: list[tuple[str, str]] = [("heading", "## " + heading)]
    paragraph = [" ".join(lines).strip() for lines in lines_by_row[1:first_indented] if lines]
    if paragraph:
        result.append(("paragraph", " ".join(paragraph)))

    indented_lefts = numeric_lefts[first_indented:]
    list_start_left = min(indented_lefts)
    ordered = not any(row.list_marker for row in group_rows[first_indented:])
    current_item: list[str] = []
    item_number = 1
    for left, lines in zip(
        indented_lefts,
        lines_by_row[first_indented:],
    ):
        text = " ".join(lines).strip()
        if not text:
            continue
        starts_item = left <= list_start_left + 8
        if starts_item and current_item:
            marker = f"{item_number}." if ordered else "-"
            result.append(("list", marker + " " + " ".join(current_item)))
            item_number += 1
            current_item = [text]
        else:
            current_item.append(text)
    if current_item:
        marker = f"{item_number}." if ordered else "-"
        result.append(("list", marker + " " + " ".join(current_item)))
    return result if len(result) >= 3 else []


def _implicit_indented_list_render(
    group_rows: list[SparseMarkdownRow],
    lines_by_row: list[list[str]],
    *,
    merge_left_count: int,
    first_content_group: bool,
    previous_group_rows: list[SparseMarkdownRow],
) -> list[tuple[str, str]]:
    if first_content_group or merge_left_count > 0 or len(group_rows) < 5 or any(row.list_marker for row in group_rows):
        return []

    current_column = min(
        (row.anchor[1] for row in group_rows),
        default=0,
    )
    previous_column = min(
        (row.anchor[1] for row in previous_group_rows),
        default=0,
    )
    if current_column == 0 and current_column <= previous_column:
        return []

    lefts = [row.content_left for row in group_rows]
    if any(left is None for left in lefts):
        return []
    numeric_lefts = [int(left) for left in lefts if left is not None]
    baseline = min(numeric_lefts)
    item_left_max = baseline + 8
    continuation_min = baseline + 12
    item_starts = [index for index, left in enumerate(numeric_lefts) if left <= item_left_max]
    continuation_count = sum(1 for left in numeric_lefts if left >= continuation_min)
    if len(item_starts) < 3 or continuation_count < 2:
        return []
    if item_starts[0] != 0:
        return []

    result: list[tuple[str, str]] = []
    for item_number, start in enumerate(item_starts, start=1):
        end = item_starts[item_number] if item_number < len(item_starts) else len(group_rows)
        text = " ".join(" ".join(lines).strip() for lines in lines_by_row[start:end] if lines).strip()
        if text:
            result.append(("list", f"{item_number}. {text}"))
    return result if len(result) >= 3 else []


def _has_heading_prefix_before_list(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    index: int,
    *,
    merge_left_count: int,
) -> bool:
    group_rows = groups[index][0]
    if merge_left_count > 0 or len(group_rows) < 8:
        return False
    if index + 1 >= len(groups):
        return False
    next_rows = groups[index + 1][0]
    if not any(row.list_marker for row in next_rows):
        return False
    first_lines = _content_lines(group_rows[0].parts)
    if len(first_lines) != 1:
        return False
    first_text = first_lines[0].strip()
    return bool(first_text) and not first_text.endswith((".", "!", "?", ";"))


def _structured_group_ahead(
    groups: list[tuple[list[SparseMarkdownRow], int, int, int]],
    index: int,
    *,
    lookahead: int,
) -> bool:
    for candidate in groups[index + 1 : index + 1 + lookahead]:
        group_rows, _, merge_left_count, _ = candidate
        if merge_left_count > 0:
            return True
        if any(row.list_marker for row in group_rows):
            return True
    return False


def _heading_level(value: str) -> int:
    match = re.match(r"^(#{1,6})\s+", value)
    return len(match.group(1)) if match else 0


def _shadow_components(
    rows: list[SparseMarkdownRow],
) -> list[tuple[list[SparseMarkdownRow], int, int, int]]:
    occupied = set()
    for row in rows:
        occupied.update(_row_shadow_cells(row))
    parent = {cell: cell for cell in occupied}

    def find(cell: tuple[int, int]) -> tuple[int, int]:
        root = cell
        while parent[root] != root:
            root = parent[root]
        while parent[cell] != cell:
            next_cell = parent[cell]
            parent[cell] = root
            cell = next_cell
        return root

    def union(
        first: tuple[int, int],
        second: tuple[int, int],
    ) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    merge_up_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    merge_left_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    strong_horizontal_rows = {row.anchor[0] for row in rows if _row_merge_left_count(row) >= 2}
    by_row: dict[int, list[tuple[int, int]]] = {}
    for cell in occupied:
        by_row.setdefault(cell[0], []).append(cell)
    for row in rows:
        starts_horizontal_table = row.anchor[0] in strong_horizontal_rows and row.anchor[0] == min(
            strong_horizontal_rows
        )
        for row_number, column, code in row.codes:
            cell = (row_number, column)
            if code in MERGE_UP_CODES:
                above = (row_number - 1, column)
                if above in parent and not starts_horizontal_table:
                    union(cell, above)
                    merge_up_edges.add((above, cell))
            if code in MERGE_LEFT_CODES:
                left_candidates = [candidate for candidate in by_row.get(row_number, []) if candidate[1] < column]
                if not left_candidates:
                    continue
                left = max(left_candidates, key=lambda candidate: candidate[1])
                union(left, cell)
                merge_left_edges.add((left, cell))

    component_cells: dict[
        tuple[int, int],
        set[tuple[int, int]],
    ] = {}
    for cell in occupied:
        component_cells.setdefault(find(cell), set()).add(cell)

    result = []
    for cells in sorted(
        component_cells.values(),
        key=lambda value: min(value),
    ):
        component_rows = [row for row in rows if row.anchor in cells]
        if not component_rows:
            continue
        up_count = sum(1 for first, second in merge_up_edges if first in cells and second in cells)
        left_count = sum(1 for first, second in merge_left_edges if first in cells and second in cells)
        result.append(
            (
                component_rows,
                up_count,
                left_count,
                0,
            )
        )
    return result


def _row_shadow_cells(row: SparseMarkdownRow) -> set[tuple[int, int]]:
    cells = {row.anchor}
    cells.update((row_number, column) for row_number, column, _ in row.codes)
    return cells


def lint_markdown_structure(markdown: str) -> tuple[str, ...]:
    errors = []
    previous_was_list = False
    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            previous_was_list = False
            continue
        if line.startswith("#"):
            marker = line.split(maxsplit=1)[0]
            is_heading = bool(re.match(r"^#{1,6}\s+\S", line))
            if re.match(r"^#{1,6}\s*$", line) or re.match(
                r"^#{7,}\s",
                line,
            ):
                errors.append(f"line {line_number}: invalid heading marker")
            if is_heading and len(line) == len(marker):
                errors.append(f"line {line_number}: empty heading")
            previous_was_list = False
        elif re.match(r"^-\s", line):
            if not re.match(r"^-\s+\S", line):
                errors.append(f"line {line_number}: invalid list marker")
            previous_was_list = True
        elif previous_was_list:
            errors.append(f"line {line_number}: unmerged list continuation")
            previous_was_list = False
        else:
            previous_was_list = False
    return tuple(errors)


def _content_lines(parts: tuple[str, ...]) -> list[str]:
    return [cleaned for part in parts for line in part.splitlines() if (cleaned := _sanitize_structural_content(line))]


def _sanitize_structural_content(value: str) -> str:
    value = value.replace("|", " ").replace("_", " ")
    value = re.sub(r"(?<!\w)=+(?!\w)", " ", value)
    value = " ".join(value.split())
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def _strip_list_marker(value: str) -> str:
    return re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", value).strip()
