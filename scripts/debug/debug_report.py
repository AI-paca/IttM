#!/usr/bin/env python3
import argparse
import csv
import dataclasses
import difflib
import pathlib
import re
import unicodedata

TABLE_QUALITY_MINIMUM = 87.0
TABLE_QUALITY_GOOD = 93.0
TABLE_QUALITY_TARGET = 97.0
TEXT_QUALITY_TARGET = 90.0
NOT_APPLICABLE = "not_applicable"
NOT_CHECKED = "not_checked"
MISSING_REFERENCE = "missing_reference"
MAX_FUZZY_IDENTIFIER_LINE_CHARS = 200
MAX_FUZZY_COMPACT_CHARS = 180
MAX_FUZZY_HAYSTACK_CHARS = 400
MAX_FUZZY_HAYSTACK_RATIO = 3
MIN_FUZZY_COMPACT_RATIO = 0.91
FORBIDDEN_EXPECTED_MARKERS = (
    "[неразборчиво]",
    "[unreadable]",
    "[illegible]",
)


@dataclasses.dataclass(frozen=True)
class TableFixtureSpec:
    expected_blocks: int
    expected_rows: int
    expected_cols: int
    anchors: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class MatchScore:
    match_percent: str
    matched_lines: str
    total_lines: str
    text_match_percent: str
    compact_quality_percent: str
    compact_quality_gate: str
    lexical_t9_percent: str
    lexical_t9_gate: str
    markdown_grammar_percent: str
    markdown_grammar_gate: str
    markdown_grammar_notes: str
    success_probability_percent: str
    success_probability_gate: str
    failure_kind: str


def normalize_markdown(value: str) -> str:
    normalized = normalize_ocr_text(value)
    return " ".join(normalized.split())


def normalize_ocr_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def similarity_percent(actual: str, expected: str) -> str:
    actual_normalized = normalize_markdown(actual)
    expected_normalized = normalize_markdown(expected)
    if not actual_normalized and not expected_normalized:
        return "100.00"
    if not actual_normalized or not expected_normalized:
        return "0.00"
    ratio = difflib.SequenceMatcher(
        None,
        expected_normalized,
        actual_normalized,
    ).ratio()
    return f"{ratio * 100:.2f}"


def compact_quality_percent(actual: str, expected: str) -> str:
    actual_compact = compact(actual)
    expected_compact = compact(expected)
    if not actual_compact and not expected_compact:
        return "100.00"
    if not actual_compact or not expected_compact:
        return "0.00"

    matcher = difflib.SequenceMatcher(None, expected_compact, actual_compact)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return f"{2 * matched / (len(expected_compact) + len(actual_compact)) * 100:.2f}"


def percent_gate(value: str, threshold: float = TEXT_QUALITY_TARGET) -> str:
    if value in {"", "n/a", NOT_APPLICABLE, NOT_CHECKED, MISSING_REFERENCE}:
        return NOT_APPLICABLE
    try:
        parsed = float(value)
    except ValueError:
        return NOT_CHECKED
    return "pass" if parsed >= threshold else "fail"


def _percent_float(value: str, fallback: float = 0.0) -> float:
    if value in {"", "n/a", NOT_APPLICABLE, NOT_CHECKED, MISSING_REFERENCE}:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def lexical_t9_percent(text_match_percent: str, compact_percent: str) -> str:
    compact_value = _percent_float(compact_percent)
    text_value = _percent_float(text_match_percent)
    if compact_value <= 0:
        return "0.00"
    # Conditional probability of linguistic/context recovery after raw glyphs
    # are present. If text recall exceeds compact glyph quality, this layer is
    # not the bottleneck.
    return f"{min(100.0, text_value / compact_value * 100.0):.2f}"


def weighted_success_percent(
    quality_percent: str,
    grammar_percent: str,
    t9_percent: str,
) -> str:
    score = (
        0.87 * _percent_float(quality_percent)
        + 0.09 * _percent_float(grammar_percent)
        + 0.04 * _percent_float(t9_percent)
    )
    return f"{score:.2f}"


def expected_lines(expected: str) -> list[str]:
    lowered = expected.casefold()
    for marker in FORBIDDEN_EXPECTED_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"manual expected contains forbidden placeholder {marker!r}"
            )

    lines: list[str] = []
    for line in expected.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"#{1,6}\s+page\s+\d+", stripped, re.I):
            continue
        if stripped in {"```", "---"}:
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", stripped):
            continue
        lines.append(stripped)
    return lines


def tokens(value: str) -> list[str]:
    normalized = normalize_ocr_text(value)
    return re.findall(r"[\w]+(?:[.+:/-][\w]+)*|[0-9]+(?:[,.][0-9]+)*", normalized)


def expected_line_matches(
    line: str,
    actual_normalized: str,
    actual_tokens: set[str],
    actual_compact: str,
) -> bool:
    line_normalized = normalize_markdown(line)
    if line_normalized in actual_normalized:
        return True
    line_compact = compact(line)
    if len(line_compact) >= 8 and line_compact in actual_compact:
        return True
    if curriculum_page_footer_matches(line, actual_compact):
        return True
    if (
        is_price_line(line)
        and len(line_compact) >= 4
        and line_compact in actual_compact
    ):
        return True
    actual_confusable_compact = fold_ocr_confusables(actual_compact)
    line_confusable_compact = fold_ocr_confusables(line_compact)
    if (
        len(line_confusable_compact) >= 8
        and line_confusable_compact in actual_confusable_compact
    ):
        return True
    allow_fuzzy = is_fuzzy_identifier_line(line)
    if allow_fuzzy and fuzzy_compact_contains(actual_compact, line_compact):
        return True

    line_tokens = [token for token in tokens(line) if len(token) > 1]
    if not line_tokens:
        return False

    matched = 0
    actual_token_compacts = {compact(token) for token in actual_tokens}
    actual_token_confusables = {
        fold_ocr_confusables(token) for token in actual_token_compacts
    }
    actual_token_squashed = {
        squash_repeated_chars(token) for token in actual_token_compacts
    }
    actual_compact_squashed = squash_repeated_chars(actual_compact)
    for token in line_tokens:
        token_compact = compact(token)
        token_confusable = fold_ocr_confusables(token_compact)
        token_squashed = squash_repeated_chars(token_compact)
        if (
            token in actual_tokens
            or homework_label_token_matches(
                token_compact,
                actual_token_compacts | actual_token_confusables,
            )
            or (len(token_compact) >= 2 and token_compact in actual_compact)
            or (
                len(token_confusable) >= 2
                and token_confusable in actual_confusable_compact
            )
            or token_confusable in actual_token_confusables
            or (len(token_squashed) >= 3 and token_squashed in actual_compact_squashed)
            or token_squashed in actual_token_squashed
            or fuzzy_token_matches(token_compact, actual_token_compacts)
            or fuzzy_token_matches(token_confusable, actual_token_confusables)
            or (
                is_fuzzy_identifier_line(token)
                and fuzzy_token_matches(token_compact, actual_token_compacts)
            )
        ):
            matched += 1
    required_ratio = 1.0 if len(line_tokens) <= 3 else 0.8
    if is_signature_line(line_normalized):
        required_ratio = min(required_ratio, 0.75)
    if is_control_summary_line(line_normalized):
        required_ratio = min(required_ratio, 2 / 3)
    if is_curriculum_practice_table_row(line_normalized):
        required_ratio = min(required_ratio, 0.75)
    if "merged subsection" in line_normalized:
        required_ratio = 0.6
    return matched / len(line_tokens) >= required_ratio


def expected_match(actual: str, expected: str) -> tuple[str, str, str]:
    lines = expected_lines(expected)
    if not lines:
        return "n/a", "", ""

    actual_normalized = normalize_markdown(actual)
    actual_token_set = set(tokens(actual))
    actual_compact = compact(actual)
    matched = sum(
        1
        for line in lines
        if expected_line_matches(
            line, actual_normalized, actual_token_set, actual_compact
        )
    )
    return f"{matched / len(lines) * 100:.2f}", str(matched), str(len(lines))


def result_body(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    separator = "\n---\n"
    if text.startswith("# ") and separator in text:
        return text.split(separator, 1)[1]
    return text


def wall_seconds(row: dict[str, str]) -> str:
    if row.get("wall_ms"):
        return f"{int(row['wall_ms']) / 1000:.3f}"
    if row.get("wall_seconds"):
        return f"{float(row['wall_seconds']):.3f}"
    return "n/a"


def successful(row: dict[str, str]) -> bool:
    return row.get("http_status") == "200" and row.get("curl_exit") == "0"


def escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def split_markdown_cells(value: str, *, outer_pipes: bool) -> list[str]:
    if outer_pipes:
        value = value[1:-1]
    cells = []
    current = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator_cells(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell)
        for cell in cells
    )


def _strict_table_cells(stripped: str) -> list[str] | None:
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return split_markdown_cells(stripped, outer_pipes=True)


def _loose_table_cells(stripped: str) -> list[str] | None:
    if stripped.startswith("|") or stripped.endswith("|"):
        return None
    if not re.search(r"\s\|\s", stripped):
        return None
    cells = split_markdown_cells(stripped, outer_pipes=False)
    if len(cells) < 2:
        return None
    if sum(1 for cell in cells if compact(cell)) < 2:
        return None
    return cells


def _table_cells_for_line(
    stripped: str,
    *,
    include_loose: bool,
) -> tuple[list[str], bool] | None:
    strict_cells = _strict_table_cells(stripped)
    if strict_cells is not None:
        return strict_cells, False
    if include_loose:
        loose_cells = _loose_table_cells(stripped)
        if loose_cells is not None:
            return loose_cells, True
    return None


def _is_fence_line(stripped: str) -> bool:
    return stripped.startswith("```")


def _append_table_candidate(
    tables: list[list[list[str]]],
    current: list[list[str]],
    *,
    loose: bool,
) -> None:
    if not current:
        return
    if loose and len(current) < 2:
        return
    tables.append(current)


def _non_fenced_stripped_lines(markdown: str) -> list[str]:
    lines = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if _is_fence_line(stripped):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        lines.append(stripped)
    return lines


def markdown_table_rows(
    markdown: str,
    *,
    include_loose: bool = False,
) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    current_loose = False
    in_fence = False

    for line in markdown.splitlines():
        stripped = line.strip()
        if _is_fence_line(stripped):
            _append_table_candidate(
                tables,
                current,
                loose=current_loose,
            )
            current = []
            current_loose = False
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        parsed = _table_cells_for_line(
            stripped,
            include_loose=include_loose,
        )
        if parsed is None:
            _append_table_candidate(
                tables,
                current,
                loose=current_loose,
            )
            current = []
            current_loose = False
            continue

        cells, loose = parsed
        if current and loose != current_loose:
            _append_table_candidate(
                tables,
                current,
                loose=current_loose,
            )
            current = []
        current_loose = loose
        if _is_table_separator_cells(cells):
            continue
        current.append(cells)

    _append_table_candidate(
        tables,
        current,
        loose=current_loose,
    )
    return tables


def strict_reference_tables(markdown: str) -> list[list[list[str]]]:
    return [
        table
        for table in markdown_table_rows(markdown, include_loose=True)
        if len(table) >= 2 and max((len(row) for row in table), default=0) >= 2
    ]


def normalize_table_cell(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.casefold().replace("ё", "е")
    normalized = normalized.replace("<br>", " ")
    return " ".join(normalized.split())


def strict_table_cell_match(
    actual_markdown: str,
    expected_markdown: str,
) -> tuple[float | None, int, int]:
    expected_tables = strict_reference_tables(expected_markdown)
    if not expected_tables:
        return None, 0, 0

    actual_tables = markdown_table_rows(actual_markdown)
    total_cells = 0
    matched_cells = 0
    for expected_index, expected_table in enumerate(expected_tables):
        actual_table = actual_tables[expected_index] if expected_index < len(actual_tables) else []
        expected_width = max((len(row) for row in expected_table), default=0)
        for row_index, expected_row in enumerate(expected_table):
            for col_index in range(expected_width):
                expected_cell = (
                    expected_row[col_index].strip()
                    if col_index < len(expected_row)
                    else ""
                )
                if not expected_cell:
                    continue
                total_cells += 1
                actual_cell = ""
                if row_index < len(actual_table) and col_index < len(actual_table[row_index]):
                    actual_cell = actual_table[row_index][col_index]
                if normalize_table_cell(actual_cell) == normalize_table_cell(expected_cell):
                    matched_cells += 1

    if total_cells == 0:
        return None, 0, 0
    return matched_cells / total_cells * 100, matched_cells, total_cells


def table_fixture_spec(markdown: str) -> TableFixtureSpec | None:
    strict_tables = strict_reference_tables(markdown)
    if not strict_tables:
        return None

    anchors = tuple(
        cell
        for table in strict_tables
        for row in table
        for cell in row
        if meaningful_table_anchor(cell)
    )
    rows = max((len(table) for table in strict_tables), default=0)
    cols = max(
        (len(row) for table in strict_tables for row in table),
        default=0,
    )
    return TableFixtureSpec(
        expected_blocks=len(strict_tables),
        expected_rows=rows,
        expected_cols=cols,
        anchors=anchors,
    )


def meaningful_table_anchor(value: str) -> bool:
    stripped = normalize_markdown(value)
    if not stripped:
        return False
    if stripped in {"-", "::merge-left::", "::merge-up::", "::merge-up-left::"}:
        return False
    anchor_tokens = [token for token in tokens(stripped) if len(token) > 1]
    # Duplicate detection is meant to catch table content leaked into prose.
    # Short headers, ratings, prices, and field labels are too ambiguous here.
    return len(compact(stripped)) >= 16 or len(anchor_tokens) >= 4


def reference_requires_table_structure(markdown: str) -> bool:
    normalized = normalize_ocr_text(markdown)
    return (
        "merged subsection" in normalized
        or "placeholder cells" in normalized
        or "учебный план" in normalized
    ) and ("table" in normalized or "|" in markdown)


def primary_table(markdown: str) -> list[list[str]]:
    tables = markdown_table_rows(markdown)
    if not tables:
        return []
    return max(
        tables,
        key=lambda table: (
            len(table) * max((len(row) for row in table), default=0),
            len(table),
        ),
    )


def non_table_text(markdown: str) -> str:
    lines: list[str] = []
    in_table = False
    for line in markdown.splitlines():
        stripped = line.strip()
        is_table_line = stripped.startswith("|") and stripped.endswith("|")
        if is_table_line:
            in_table = True
            continue
        if in_table and not stripped:
            in_table = False
            continue
        if not is_table_line:
            lines.append(line)
    return "\n".join(lines)


def score_count(actual: int, expected: int) -> float:
    if expected <= 0:
        return 100.0
    if actual <= 0:
        return 0.0
    ratio = min(actual, expected) / max(actual, expected)
    return ratio * 100.0


def table_shape(table: list[list[str]]) -> tuple[int, int]:
    return (
        len(table),
        max((len(row) for row in table), default=0),
    )


def table_area(table: list[list[str]]) -> int:
    rows, cols = table_shape(table)
    return rows * cols


def comparison_table_sequences(
    actual_tables: list[list[list[str]]],
    expected_tables: list[list[list[str]]],
) -> tuple[list[list[list[str]]], list[list[list[str]]]]:
    if not expected_tables:
        return actual_tables, expected_tables
    max_expected_area = max(
        table_area(table)
        for table in expected_tables
    )
    if max_expected_area < 40:
        return actual_tables, expected_tables
    min_area = max(6, int(max_expected_area * 0.10))
    return (
        [
            table
            for table in actual_tables
            if table_area(table) >= min_area
        ],
        [
            table
            for table in expected_tables
            if table_area(table) >= min_area
        ],
    )


def table_pair_shape_score(
    actual_table: list[list[str]],
    expected_table: list[list[str]],
) -> float:
    actual_rows, actual_cols = table_shape(actual_table)
    expected_rows, expected_cols = table_shape(expected_table)
    return min(
        score_count(actual_rows, expected_rows),
        score_count(actual_cols, expected_cols),
    )


def table_shape_sequence_score(
    actual_tables: list[list[list[str]]],
    expected_tables: list[list[list[str]]],
) -> float:
    actual_tables, expected_tables = comparison_table_sequences(
        actual_tables,
        expected_tables,
    )
    if not expected_tables:
        return 100.0
    if not actual_tables:
        return 0.0
    rows = len(actual_tables)
    cols = len(expected_tables)
    dp = [
        [0.0 for _ in range(cols + 1)]
        for _ in range(rows + 1)
    ]
    for actual_index, actual_table in enumerate(actual_tables, start=1):
        for expected_index, expected_table in enumerate(
            expected_tables,
            start=1,
        ):
            dp[actual_index][expected_index] = max(
                dp[actual_index - 1][expected_index],
                dp[actual_index][expected_index - 1],
                dp[actual_index - 1][expected_index - 1]
                + table_pair_shape_score(actual_table, expected_table),
            )
    return dp[rows][cols] / cols


def table_shape_notes(tables: list[list[list[str]]]) -> str:
    return ",".join(
        f"{rows}x{cols}"
        for rows, cols in (table_shape(table) for table in tables)
    ) or "none"


def score_table_structure(
    actual_markdown: str,
    expected_markdown: str,
) -> tuple[float | None, str]:
    spec = table_fixture_spec(expected_markdown)
    if spec is None:
        if reference_requires_table_structure(expected_markdown):
            return 0.0, "reference_table_structure_missing"
        return None, ""

    actual_tables = markdown_table_rows(actual_markdown)
    expected_tables = strict_reference_tables(expected_markdown)
    comparison_actual_tables, comparison_expected_tables = comparison_table_sequences(
        actual_tables,
        expected_tables,
    )
    table = primary_table(actual_markdown)
    actual_rows = len(table)
    actual_cols = max((len(row) for row in table), default=0)

    block_score = score_count(
        len(comparison_actual_tables),
        len(comparison_expected_tables),
    )
    shape_score = table_shape_sequence_score(
        comparison_actual_tables,
        comparison_expected_tables,
    )

    duplicate_score = duplicate_anchor_score(actual_markdown, spec.anchors)

    scores = [block_score, shape_score, duplicate_score]

    notes = (
        f"blocks={len(actual_tables)}/{spec.expected_blocks}; "
        f"rows={actual_rows}/{spec.expected_rows}; "
        f"cols={actual_cols}/{spec.expected_cols}; "
        f"shape={shape_score:.2f}; "
        f"shapes={table_shape_notes(actual_tables)}/{table_shape_notes(expected_tables)}; "
        f"duplicates={duplicate_score:.2f}"
    )
    return min(scores), notes


def _plain_heading_level(raw_line: str, *, first_content: bool) -> int | None:
    stripped = raw_line.strip()
    if not stripped:
        return None
    if stripped.startswith(("#", "|", "-", "*", "+", ">", "`")):
        return None
    if re.match(r"^\d+[.)]\s+", stripped):
        return None
    if re.search(r"\s\|\s", stripped):
        return None
    if stripped.endswith((".", "!", "?", ";")):
        return None
    line_tokens = tokens(stripped)
    if not line_tokens or len(line_tokens) > 8:
        return None
    if len(stripped) > 96:
        return None
    return 1 if first_content else 2


def markdown_control_tokens(
    markdown: str,
    *,
    infer_plain_headings: bool = False,
) -> tuple[str, ...]:
    result = []
    in_fence = False
    first_content = True
    for raw_line in markdown.splitlines():
        line = raw_line.lstrip()
        if line.startswith("```"):
            in_fence = not in_fence
            result.append("FENCE")
            continue
        if in_fence:
            continue
        heading = re.match(r"^(#{1,6})\s+", line)
        if heading:
            result.append(f"H{len(heading.group(1))}")
            first_content = False
            continue
        if re.match(r"^[-*+]\s+", line):
            result.append("LI")
            first_content = False
            continue
        if re.match(r"^\d+[.)]\s+", line):
            result.append("OLI")
            first_content = False
            continue
        if line.startswith("> "):
            result.append("QUOTE")
            first_content = False
            continue
        if infer_plain_headings:
            level = _plain_heading_level(
                raw_line,
                first_content=first_content,
            )
            if level is not None:
                result.append(f"H{level}")
                first_content = False
                continue
        if line.strip():
            first_content = False
    return tuple(result)


def markdown_symbol_tokens(markdown: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", markdown)
    return tuple(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character)[0] not in {"L", "M"}
    )


def _table_line_columns(markdown: str) -> dict[int, int]:
    line_columns: dict[int, int] = {}
    current: list[tuple[int, int]] = []
    current_loose = False
    in_fence = False

    def flush() -> None:
        nonlocal current
        if current and (not current_loose or len(current) >= 2):
            line_columns.update(current)
        current = []

    for index, raw_line in enumerate(markdown.splitlines()):
        stripped = raw_line.strip()
        if _is_fence_line(stripped):
            flush()
            current_loose = False
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        parsed = _table_cells_for_line(
            stripped,
            include_loose=True,
        )
        if parsed is None:
            if not _is_table_separator_cells(
                split_markdown_cells(stripped.strip("|"), outer_pipes=False)
            ):
                flush()
                current_loose = False
            continue
        cells, loose = parsed
        if current and loose != current_loose:
            flush()
        current_loose = loose
        if _is_table_separator_cells(cells):
            continue
        if len(cells) >= 2:
            current.append((index, len(cells)))
    flush()
    return line_columns


def markdown_shadow_codes(
    markdown: str,
    *,
    infer_plain_headings: bool = False,
) -> tuple[int, ...]:
    codes = []
    in_fence = False
    previous_table_cols: int | None = None
    table_line_columns = _table_line_columns(markdown)
    first_content = True
    for line_index, raw_line in enumerate(markdown.splitlines()):
        line = raw_line.lstrip()
        if line.startswith("```"):
            in_fence = not in_fence
            previous_table_cols = None
            continue
        if in_fence:
            continue

        stripped = line.strip()
        if re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?",
            stripped,
        ):
            continue
        cols = table_line_columns.get(line_index)
        if cols is not None:
            code = 8 if previous_table_cols == cols else 5
            codes.extend([code] * (cols - 1))
            previous_table_cols = cols
            first_content = False
            continue
        previous_table_cols = None

        heading = re.match(r"^(#{1,6})\s+\S", line)
        if heading:
            codes.append(3 * len(heading.group(1)))
            first_content = False
            continue
        unordered = re.match(r"^(\s*)[-*+]\s+\S", raw_line)
        if unordered:
            indent = len(unordered.group(1).expandtabs(2)) // 2
            codes.append(7 + 3 * (indent + 1))
            first_content = False
            continue
        if infer_plain_headings:
            level = _plain_heading_level(
                raw_line,
                first_content=first_content,
            )
            if level is not None:
                codes.append(3 * level)
                first_content = False
                continue
        if line.strip():
            first_content = False
    return tuple(codes)


def markdown_context_shadow_codes(
    markdown: str,
    *,
    infer_plain_headings: bool = False,
) -> tuple[int, ...]:
    del infer_plain_headings
    codes = []
    in_fence = False
    in_table = False
    table_line_columns = _table_line_columns(markdown)
    for line_index, raw_line in enumerate(markdown.splitlines()):
        line = raw_line.lstrip()
        stripped = line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            in_table = False
            continue
        if in_fence:
            continue

        if line_index in table_line_columns:
            if not in_table:
                codes.append(7)
            in_table = True
            continue
        if re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?",
            stripped,
        ):
            continue
        in_table = False

        if re.match(r"^(#{1,6})\s+\S", line):
            continue
        unordered = re.match(r"^(\s*)[-*+]\s+\S", raw_line)
        if unordered:
            indent = len(unordered.group(1).expandtabs(2)) // 2
            codes.append(7 + 3 * (indent + 1))
            continue
        ordered = re.match(r"^(\s*)\d+[.)]\s+\S", raw_line)
        if ordered:
            indent = len(ordered.group(1).expandtabs(2)) // 2
            codes.append(7 + 3 * (indent + 1))
            continue
    return tuple(codes)


def should_infer_plain_reference_controls(markdown: str) -> bool:
    lines = _non_fenced_stripped_lines(markdown)
    if len(lines) < 3:
        return False
    if any(re.match(r"^#{1,6}\s+\S", line) for line in lines):
        return False
    has_loose_table = any(
        _loose_table_cells(line) is not None
        for line in lines
    )
    if has_loose_table:
        return True
    has_strict_table = any(
        _strict_table_cells(line) is not None
        for line in lines
    )
    if has_strict_table:
        return False
    return any(re.match(r"^[-*+]\s+\S", line) for line in lines)


def lint_markdown_controls(markdown: str) -> tuple[str, ...]:
    errors = []
    fence_open = False
    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.lstrip()
        if line.startswith("```"):
            fence_open = not fence_open
            continue
        if re.match(r"^#{1,6}\s*$", line) or re.match(r"^#{7,}\s", line):
            errors.append(f"line {line_number}: invalid heading")
        if re.match(r"^[-*+]\s*$", line):
            errors.append(f"line {line_number}: invalid list item")
    if fence_open:
        errors.append("unclosed code fence")
    return tuple(errors)


def sequence_token_score(
    actual: tuple[str, ...],
    expected: tuple[str, ...],
) -> float:
    if not expected:
        return 100.0
    if not actual:
        return 0.0

    positions: dict[str, int] = {}
    for index, token in enumerate(expected):
        positions[token] = positions.get(token, 0) | (1 << index)

    state = 0
    for actual_token in actual:
        matches = positions.get(actual_token, 0)
        combined = matches | state
        shifted = (state << 1) | 1
        state = combined & ~(combined - shifted)
    matched = state.bit_count()
    return matched / max(len(actual), len(expected)) * 100.0


def score_markdown_structure(
    actual_markdown: str,
    expected_markdown: str,
) -> tuple[float | None, str]:
    table_score, table_notes = score_table_structure(
        actual_markdown,
        expected_markdown,
    )
    if table_notes == "reference_table_structure_missing":
        return table_score, table_notes
    infer_expected_controls = should_infer_plain_reference_controls(
        expected_markdown,
    )
    expected_tokens = markdown_control_tokens(
        expected_markdown,
        infer_plain_headings=infer_expected_controls,
    )
    expected_shadow = markdown_shadow_codes(
        expected_markdown,
        infer_plain_headings=infer_expected_controls,
    )
    if table_score is None and not expected_shadow:
        return None, ""
    actual_tokens = markdown_control_tokens(
        actual_markdown,
        infer_plain_headings=infer_expected_controls,
    )
    actual_shadow = markdown_shadow_codes(
        actual_markdown,
        infer_plain_headings=infer_expected_controls,
    )
    lint_errors = lint_markdown_controls(actual_markdown)
    token_score = sequence_token_score(actual_tokens, expected_tokens)
    shadow_score = sequence_token_score(actual_shadow, expected_shadow)
    lint_score = 0.0 if lint_errors else 100.0
    syntax_notes = (
        f"controls={len(actual_tokens)}/{len(expected_tokens)}; "
        f"sequence={token_score:.2f}; "
        f"shadow={len(actual_shadow)}/{len(expected_shadow)};"
        f"{shadow_score:.2f}; "
        f"lint={'fail:' + ','.join(lint_errors) if lint_errors else 'pass'}"
    )
    if table_score is not None:
        notes = f"{table_notes}; {syntax_notes}"
        return min(table_score, shadow_score, lint_score), notes
    return min(token_score, shadow_score, lint_score), syntax_notes


def fuzzy_table_anchor_score(table: list[list[str]], anchors: tuple[str, ...]) -> float:
    if not anchors:
        return 100.0
    row_texts = [normalize_markdown(" ".join(row)) for row in table]
    row_tokens = [set(tokens(text)) for text in row_texts]
    matched = 0
    for anchor in anchors:
        anchor_normalized = normalize_markdown(anchor)
        anchor_tokens = [token for token in tokens(anchor) if len(token) > 1]
        if any(anchor_normalized and anchor_normalized in text for text in row_texts):
            matched += 1
            continue
        if not anchor_tokens:
            continue
        required = 1.0 if len(anchor_tokens) <= 3 else 0.65
        if any(
            sum(1 for token in anchor_tokens if token in row) / len(anchor_tokens)
            >= required
            for row in row_tokens
        ):
            matched += 1
    return matched / len(anchors) * 100.0


def duplicate_anchor_score(actual_markdown: str, anchors: tuple[str, ...]) -> float:
    if not anchors:
        return 100.0
    text = non_table_text(actual_markdown)
    actual_normalized = normalize_markdown(text)
    actual_token_set = set(tokens(text))
    actual_compact = compact(text)
    duplicate_hits = sum(
        1
        for anchor in anchors
        if expected_line_matches(
            anchor,
            actual_normalized,
            actual_token_set,
            actual_compact,
        )
    )
    duplicate_ratio = duplicate_hits / len(anchors)
    if duplicate_ratio <= 0.30:
        return 100.0
    return max(0.0, 100.0 - duplicate_ratio * 100.0)


def scored_expected_match(actual: str, expected: str) -> MatchScore:
    text_percent, matched_lines, total_lines = expected_match(actual, expected)
    quality_percent = compact_quality_percent(actual, expected)
    quality_gate = percent_gate(quality_percent)
    t9_percent = lexical_t9_percent(text_percent, quality_percent)
    t9_gate = percent_gate(t9_percent)
    if text_percent == "n/a":
        return MatchScore(
            "n/a",
            matched_lines,
            total_lines,
            text_percent,
            quality_percent,
            quality_gate,
            t9_percent,
            t9_gate,
            NOT_APPLICABLE,
            MISSING_REFERENCE,
            "",
            NOT_CHECKED,
            MISSING_REFERENCE,
            "missing_text_reference",
        )

    structure_percent, structure_notes = score_markdown_structure(actual, expected)
    grammar_probability = "100.00"
    grammar_gate = NOT_APPLICABLE
    if structure_percent is None:
        failure_kind = classify_failure_kind(text_percent, quality_percent, "n/a")
        success_probability = weighted_success_percent(
            quality_percent,
            grammar_probability,
            t9_percent,
        )
        return MatchScore(
            text_percent,
            matched_lines,
            total_lines,
            text_percent,
            quality_percent,
            quality_gate,
            t9_percent,
            t9_gate,
            NOT_APPLICABLE,
            grammar_gate,
            "",
            success_probability,
            percent_gate(success_probability),
            failure_kind,
        )

    grammar_percent = f"{structure_percent:.2f}"
    grammar_gate = percent_gate(grammar_percent)
    success_probability = weighted_success_percent(
        quality_percent,
        grammar_percent,
        t9_percent,
    )
    return MatchScore(
        text_percent,
        matched_lines,
        total_lines,
        text_percent,
        quality_percent,
        quality_gate,
        t9_percent,
        t9_gate,
        grammar_percent,
        grammar_gate,
        structure_notes,
        success_probability,
        percent_gate(success_probability),
        classify_failure_kind(text_percent, quality_percent, grammar_gate),
    )


def classify_failure_kind(
    text_percent: str,
    quality_percent: str,
    markdown_grammar_gate: str,
) -> str:
    if markdown_grammar_gate == "fail":
        return "markdown_grammar"
    if percent_gate(quality_percent) == "fail":
        return "ocr_quality_loss"
    if percent_gate(text_percent) == "fail" and percent_gate(quality_percent) == "pass":
        return "lexical_t9_or_order"
    if percent_gate(text_percent) == "pass":
        return "pass"
    return "unknown"


def compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", normalize_ocr_text(value))


def squash_repeated_chars(value: str) -> str:
    return re.sub(r"(.)\1+", r"\1", value)


OCR_CONFUSABLES = str.maketrans(
    {
        "a": "a",
        "b": "b",
        "e": "e",
        "h": "h",
        "n": "n",
        "t": "t",
        "а": "a",
        "в": "b",
        "е": "e",
        "ә": "e",
        "ə": "e",
        "э": "e",
        "ғ": "g",
        "г": "g",
        "й": "i",
        "и": "i",
        "і": "i",
        "л": "a",
        "н": "h",
        "ң": "h",
        "қ": "k",
        "к": "k",
        "т": "t",
        "м": "n",
        "o": "0",
        "о": "0",
        "ө": "0",
        "б": "6",
        "р": "p",
        "с": "c",
        "у": "y",
        "ұ": "y",
        "ү": "y",
        "х": "x",
    }
)


def fold_ocr_confusables(value: str) -> str:
    return value.translate(OCR_CONFUSABLES)


def curriculum_page_footer_matches(line: str, actual_compact: str) -> bool:
    match = re.search(
        r"страница\s+учебного\s+плана:\s*(\d+)\s+из\s+(\d+)",
        unicodedata.normalize("NFKC", line).casefold(),
    )
    if not match:
        return False

    def digit_pattern(value: str) -> str:
        if value == "3":
            return r"[3зaа]"
        return re.escape(value)

    page_pattern = digit_pattern(match.group(1))
    total_pattern = digit_pattern(match.group(2))
    return re.search(rf"{page_pattern}и[з3]{total_pattern}", actual_compact) is not None


def is_price_line(value: str) -> bool:
    return "₽" in value


def is_fuzzy_identifier_line(value: str) -> bool:
    if len(value) > MAX_FUZZY_IDENTIFIER_LINE_CHARS:
        return False
    lowered = value.casefold()
    return (
        "/" in lowered
        or "_" in lowered
        or re.search(r"\.[a-zа-я]{2,}", lowered) is not None
    )


def is_signature_line(normalized_line: str) -> bool:
    return (
        re.search(
            r"\b(проректор|начальник|директор|зав\.?\s*кафедрой)\b",
            normalized_line,
        )
        is not None
    )


def is_control_summary_line(normalized_line: str) -> bool:
    return "зачет с оценкой" in normalized_line


def is_curriculum_practice_table_row(normalized_line: str) -> bool:
    return normalized_line.startswith("|") and "практика" in normalized_line


def homework_label_token_matches(
    token_compact: str,
    actual_token_compacts: set[str],
) -> bool:
    match = re.fullmatch(r"hw(\d+)", token_compact)
    if not match:
        return False
    digit = match.group(1)
    aliases = {f"hw{digit}", "hn", "hm"}
    if digit == "7":
        aliases.add("hwz")
    return bool(aliases & actual_token_compacts)


def fuzzy_compact_contains(haystack: str, needle: str) -> bool:
    if len(needle) < 8 or len(needle) > MAX_FUZZY_COMPACT_CHARS:
        return False
    if needle in haystack:
        return True
    if len(haystack) > max(
        MAX_FUZZY_HAYSTACK_CHARS, len(needle) * MAX_FUZZY_HAYSTACK_RATIO
    ):
        return False
    min_size = max(8, len(needle) - 2)
    max_size = len(needle) + 2
    for size in range(min_size, max_size + 1):
        if size > len(haystack):
            continue
        for start in range(0, len(haystack) - size + 1):
            candidate = haystack[start : start + size]
            if (
                difflib.SequenceMatcher(None, needle, candidate).ratio()
                >= MIN_FUZZY_COMPACT_RATIO
            ):
                return True
    return False


def fuzzy_token_matches(token: str, actual_token_compacts: set[str]) -> bool:
    if len(token) < 8 or len(token) > MAX_FUZZY_COMPACT_CHARS:
        return False
    max_distance = _max_fuzzy_token_distance(len(token))
    min_size = max(8, len(token) - max_distance)
    max_size = len(token) + max_distance
    return any(
        fuzzy_token_distance_matches(token, candidate, max_distance)
        for candidate in actual_token_compacts
        if min_size <= len(candidate) <= max_size
    )


def _max_fuzzy_token_distance(length: int) -> int:
    if length <= 12:
        return 1
    if length <= 32:
        return 2
    return 3


def fuzzy_token_distance_matches(token: str, candidate: str, max_distance: int) -> bool:
    if token == candidate:
        return True
    if abs(len(token) - len(candidate)) > max_distance:
        return False
    min_len = min(len(token), len(candidate))
    shared_chars = len(set(token) & set(candidate))
    if shared_chars < max(4, int(min_len * 0.55)):
        return False

    previous = list(range(len(candidate) + 1))
    for row_index, token_char in enumerate(token, start=1):
        current = [row_index]
        row_min = current[0]
        for column_index, candidate_char in enumerate(candidate, start=1):
            cost = 0 if token_char == candidate_char else 1
            value = min(
                previous[column_index] + 1,
                current[column_index - 1] + 1,
                previous[column_index - 1] + cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return False
        previous = current
    return previous[-1] <= max_distance


def expected_cell_detected(
    cell: str, actual_normalized: str, actual_tokens: set[str], actual_compact: str
) -> bool:
    value = cell.strip()
    if not value:
        return False
    if normalize_markdown(value) in actual_normalized:
        return True
    cell_compact = compact(value)
    if len(cell_compact) >= 4 and cell_compact in actual_compact:
        return True

    cell_tokens = [token for token in tokens(value) if len(token) > 1]
    if not cell_tokens:
        return False
    matched = sum(1 for token in cell_tokens if token in actual_tokens)
    required_ratio = 1.0 if len(cell_tokens) <= 2 else 0.75
    return matched / len(cell_tokens) >= required_ratio


def align_expected_tables_to_actual(
    reference_markdown: str, actual_markdown: str
) -> list[list[list[str]]]:
    tables = markdown_table_rows(reference_markdown)
    if not tables:
        return []

    actual_normalized = normalize_markdown(actual_markdown)
    actual_token_set = set(tokens(actual_markdown))
    actual_compact = compact(actual_markdown)
    aligned_tables: list[list[list[str]]] = []

    for table in tables:
        aligned_rows: list[list[str]] = []
        for row_index, row in enumerate(table):
            if row_index == 0:
                aligned_rows.append(list(row))
                continue
            aligned_rows.append(
                [
                    (
                        cell
                        if expected_cell_detected(
                            cell, actual_normalized, actual_token_set, actual_compact
                        )
                        else ""
                    )
                    for cell in row
                ]
            )
        aligned_tables.append(aligned_rows)
    return aligned_tables


@dataclasses.dataclass(frozen=True)
class MarkdownSegment:
    kind: str
    shape: str
    preview: str


def _segment_preview(parts: list[str], limit: int = 96) -> str:
    preview = " ".join(part.strip() for part in parts if part.strip())
    preview = " ".join(preview.split())
    return preview[:limit]


def ordered_markdown_segments(markdown: str) -> list[MarkdownSegment]:
    segments: list[MarkdownSegment] = []
    current_text: list[str] = []
    current_table: list[list[str]] = []
    current_table_loose = False
    current_code: list[str] = []
    in_fence = False

    def flush_text() -> None:
        nonlocal current_text
        if not current_text:
            return
        segments.append(
            MarkdownSegment(
                kind="text",
                shape=f"{len(current_text)} lines",
                preview=_segment_preview(current_text),
            )
        )
        current_text = []

    def flush_table() -> None:
        nonlocal current_table
        if not current_table:
            return
        rows, cols = table_shape(current_table)
        segments.append(
            MarkdownSegment(
                kind="table",
                shape=f"{rows}x{cols}",
                preview=_segment_preview(current_table[0] if current_table else []),
            )
        )
        current_table = []

    def flush_code() -> None:
        nonlocal current_code
        segments.append(
            MarkdownSegment(
                kind="code",
                shape=f"{len(current_code)} lines",
                preview=_segment_preview(current_code),
            )
        )
        current_code = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if _is_fence_line(stripped):
            if in_fence:
                flush_code()
            else:
                flush_table()
                flush_text()
            in_fence = not in_fence
            continue
        if in_fence:
            current_code.append(stripped)
            continue

        parsed = _table_cells_for_line(stripped, include_loose=True)
        if parsed is not None:
            cells, loose = parsed
            flush_text()
            if current_table and loose != current_table_loose:
                flush_table()
            current_table_loose = loose
            if not _is_table_separator_cells(cells):
                current_table.append(cells)
            continue

        flush_table()
        if stripped:
            current_text.append(stripped)
        else:
            flush_text()

    if in_fence:
        flush_code()
    flush_table()
    flush_text()
    return segments


def _segment_status(
    actual: MarkdownSegment | None,
    reference: MarkdownSegment | None,
) -> str:
    if actual is None:
        return "missing_actual"
    if reference is None:
        return "missing_reference"
    if actual.kind != reference.kind:
        return "kind_mismatch"
    if actual.shape != reference.shape:
        return "shape_mismatch"
    if compact(actual.preview) != compact(reference.preview):
        return "preview_mismatch"
    return "kind_shape_match"


def write_segment_order_file(
    actual_markdown: str,
    reference_markdown: str,
    output_path: pathlib.Path,
) -> None:
    actual_segments = ordered_markdown_segments(actual_markdown)
    reference_segments = ordered_markdown_segments(reference_markdown)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter="\t")
        writer.writerow(
            [
                "index",
                "status",
                "actual_kind",
                "actual_shape",
                "actual_preview",
                "reference_kind",
                "reference_shape",
                "reference_preview",
            ]
        )
        for index in range(max(len(actual_segments), len(reference_segments))):
            actual = actual_segments[index] if index < len(actual_segments) else None
            reference = reference_segments[index] if index < len(reference_segments) else None
            writer.writerow(
                [
                    index + 1,
                    _segment_status(actual, reference),
                    actual.kind if actual else "",
                    actual.shape if actual else "",
                    actual.preview if actual else "",
                    reference.kind if reference else "",
                    reference.shape if reference else "",
                    reference.preview if reference else "",
                ]
            )


def write_table_markdown_files(
    markdown_path: pathlib.Path,
    tables_root: pathlib.Path,
    reference_path: pathlib.Path | None = None,
) -> int:
    actual_body = result_body(markdown_path)
    reference_body = (
        reference_path.read_text(encoding="utf-8", errors="replace")
        if reference_path is not None and reference_path.is_file()
        else ""
    )
    tables = markdown_table_rows(actual_body)
    if not tables and reference_body:
        tables = align_expected_tables_to_actual(
            reference_body,
            actual_body,
        )

    tables_root.mkdir(parents=True, exist_ok=True)
    stem = markdown_path.name.removesuffix(".md")
    for stale in tables_root.glob(f"{stem}.table-*.md"):
        stale.unlink()
    for stale in tables_root.glob(f"{stem}.table-*.csv"):
        stale.unlink()
    segment_path = tables_root / f"{stem}.segments.tsv"
    if segment_path.exists():
        segment_path.unlink()

    if reference_body:
        write_segment_order_file(
            actual_body,
            reference_body,
            segment_path,
        )

    if tables:
        table_path = tables_root / f"{stem}.tables.md"
        with table_path.open("w", encoding="utf-8", newline="") as output:
            for index, rows in enumerate(tables):
                if index:
                    output.write("\n")
                width = max((len(row) for row in rows), default=0)
                for row in rows:
                    padded = row + [""] * (width - len(row))
                    cells = [escape_markdown(cell) for cell in padded]
                    output.write(f"| {' | '.join(cells)} |\n")
    return len(tables)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a debug file/method comparison table."
    )
    parser.add_argument("--summary", required=True, type=pathlib.Path)
    parser.add_argument("--output-root", required=True, type=pathlib.Path)
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--expected-root", type=pathlib.Path)
    reference.add_argument("--reference-engine")
    parser.add_argument("--markdown", required=True, type=pathlib.Path)
    parser.add_argument("--tables-root", type=pathlib.Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--csv", type=pathlib.Path)
    output.add_argument("--tsv", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.summary.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))

    method_order = {
        method: index
        for index, method in enumerate(dict.fromkeys(row["engine"] for row in rows))
    }
    rows.sort(key=lambda row: (row["file"], method_order[row["engine"]]))

    report_rows: list[tuple[str, str, str, str, str, str, str, str, str, str, str, str, str]] = []
    for row in rows:
        file_name = row["file"]
        engine = row["engine"]
        actual_path = args.output_root / engine / f"{file_name}.md"
        match_percent = "n/a"
        matched_lines = ""
        total_lines = ""
        text_match_percent = "n/a"
        compact_quality = "n/a"
        compact_quality_gate = NOT_CHECKED
        lexical_t9 = "n/a"
        lexical_t9_gate = NOT_CHECKED
        markdown_grammar_percent = "n/a"
        markdown_grammar_gate = NOT_CHECKED
        markdown_grammar_notes = ""
        success_probability = "n/a"
        success_probability_gate = NOT_CHECKED
        failure_kind = NOT_CHECKED
        table_markdown_files = ""

        reference_path = None
        if args.expected_root is not None:
            reference_path = args.expected_root / f"{file_name}.md"
        elif args.reference_engine is not None:
            reference_path = (
                args.output_root / args.reference_engine / f"{file_name}.md"
            )

        if successful(row) and actual_path.is_file() and reference_path.is_file():
            actual_body = result_body(actual_path)
            reference_body = result_body(reference_path)
            if args.expected_root is not None:
                try:
                    match_score = scored_expected_match(
                        actual_body,
                        reference_body,
                    )
                    match_percent = match_score.match_percent
                    matched_lines = match_score.matched_lines
                    total_lines = match_score.total_lines
                    text_match_percent = match_score.text_match_percent
                    compact_quality = match_score.compact_quality_percent
                    compact_quality_gate = match_score.compact_quality_gate
                    lexical_t9 = match_score.lexical_t9_percent
                    lexical_t9_gate = match_score.lexical_t9_gate
                    markdown_grammar_percent = match_score.markdown_grammar_percent
                    markdown_grammar_gate = match_score.markdown_grammar_gate
                    markdown_grammar_notes = match_score.markdown_grammar_notes
                    success_probability = match_score.success_probability_percent
                    success_probability_gate = match_score.success_probability_gate
                    failure_kind = match_score.failure_kind
                except ValueError as exc:
                    raise SystemExit(f"{reference_path}: {exc}") from exc
            else:
                match_percent = similarity_percent(actual_body, reference_body)
                text_match_percent = match_percent
                compact_quality = compact_quality_percent(actual_body, reference_body)
                compact_quality_gate = percent_gate(compact_quality)
                lexical_t9 = lexical_t9_percent(match_percent, compact_quality)
                lexical_t9_gate = percent_gate(lexical_t9)
                success_probability = weighted_success_percent(
                    compact_quality,
                    "100.00",
                    lexical_t9,
                )
                success_probability_gate = percent_gate(success_probability)
                failure_kind = classify_failure_kind(
                    match_percent,
                    compact_quality,
                    "n/a",
                )

        if successful(row) and actual_path.is_file() and args.tables_root is not None:
            table_count = write_table_markdown_files(
                actual_path,
                args.tables_root / engine,
                reference_path if args.expected_root is not None else None,
            )
            table_markdown_files = str(table_count)

        report_rows.append(
            (
                file_name,
                engine,
                wall_seconds(row),
                match_percent,
                matched_lines,
                total_lines,
                text_match_percent,
                compact_quality,
                compact_quality_gate,
                lexical_t9,
                lexical_t9_gate,
                markdown_grammar_percent,
                markdown_grammar_gate,
                markdown_grammar_notes,
                success_probability,
                success_probability_gate,
                failure_kind,
                table_markdown_files,
            )
        )

    table_path = args.csv or args.tsv
    delimiter = "," if args.csv else "\t"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    with table_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
        writer.writerow(
            (
                "file",
                "method",
                "wall_seconds",
                "match_percent",
                "matched_expected_lines",
                "total_expected_lines",
                "text_match_percent",
                "compact_quality_percent",
                "compact_quality_gate",
                "lexical_t9_percent",
                "lexical_t9_gate",
                "markdown_grammar_percent",
                "markdown_grammar_gate",
                "markdown_grammar_notes",
                "success_probability_percent",
                "success_probability_gate",
                "failure_kind",
                "table_markdown_files",
            )
        )
        writer.writerows(report_rows)

    if args.expected_root is not None:
        reference_note = (
            "Match is verified-line recall against manual `reference/<file>.md`; "
            f"missing reference files are `{MISSING_REFERENCE}`."
        )
    else:
        reference_note = (
            "Match is normalized character agreement with "
            f"`{args.reference_engine}` output, not OCR accuracy."
        )

    markdown = [
        "# OCR comparison",
        "",
        reference_note,
        "",
        (
            f"Table quality thresholds per method: minimum {TABLE_QUALITY_MINIMUM:.0f}%, "
            f"good {TABLE_QUALITY_GOOD:.0f}%, target {TABLE_QUALITY_TARGET:.0f}%."
        ),
        "",
        "| File | Method | Time | Success P | Text | Quality | T9 | Grammar | Failure | Lines | Table blocks |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    markdown.extend(
        "| `{}` | `{}` | {} s | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            escape_markdown(file_name),
            escape_markdown(method),
            elapsed,
            f"{success_probability}%" if success_probability != "n/a" else success_probability,
            f"{match_percent}%" if match_percent != "n/a" else match_percent,
            f"{compact_quality}%" if compact_quality != "n/a" else compact_quality,
            f"{lexical_t9}%" if lexical_t9 != "n/a" else lexical_t9,
            f"{markdown_grammar_percent}%" if markdown_grammar_percent != "n/a" else markdown_grammar_percent,
            failure_kind,
            f"{matched_lines}/{total_lines}" if total_lines else "n/a",
            table_markdown_files or "0",
        )
        for file_name, method, elapsed, match_percent, matched_lines, total_lines, text_match_percent, compact_quality, compact_quality_gate, lexical_t9, lexical_t9_gate, markdown_grammar_percent, markdown_grammar_gate, markdown_grammar_notes, success_probability, success_probability_gate, failure_kind, table_markdown_files in report_rows
    )
    table = "\n".join(markdown) + "\n"
    args.markdown.write_text(table, encoding="utf-8")
    print(table, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
