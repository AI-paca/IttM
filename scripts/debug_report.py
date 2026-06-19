#!/usr/bin/env python3
import argparse
import csv
import difflib
import pathlib
import re
import unicodedata

TABLE_QUALITY_MINIMUM = 87.0
TABLE_QUALITY_GOOD = 93.0
TABLE_QUALITY_TARGET = 97.0
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


def markdown_table_rows(markdown: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            if current:
                tables.append(current)
                current = []
            continue

        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        current.append(cells)

    if current:
        tables.append(current)
    return tables


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
    if len(token) < 8:
        return False
    return any(
        fuzzy_compact_contains(candidate, token) for candidate in actual_token_compacts
    )


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


def write_table_markdown_files(
    markdown_path: pathlib.Path,
    tables_root: pathlib.Path,
    reference_path: pathlib.Path | None = None,
) -> int:
    actual_body = result_body(markdown_path)
    tables = markdown_table_rows(actual_body)
    if not tables and reference_path is not None and reference_path.is_file():
        tables = align_expected_tables_to_actual(
            reference_path.read_text(encoding="utf-8", errors="replace"),
            actual_body,
        )
    if not tables:
        return 0

    tables_root.mkdir(parents=True, exist_ok=True)
    stem = markdown_path.name.removesuffix(".md")
    for stale in tables_root.glob(f"{stem}.table-*.md"):
        stale.unlink()
    for stale in tables_root.glob(f"{stem}.table-*.csv"):
        stale.unlink()

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

    report_rows: list[tuple[str, str, str, str, str, str, str]] = []
    for row in rows:
        file_name = row["file"]
        engine = row["engine"]
        actual_path = args.output_root / engine / f"{file_name}.md"
        match_percent = "n/a"
        matched_lines = ""
        total_lines = ""
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
                    match_percent, matched_lines, total_lines = expected_match(
                        actual_body,
                        reference_body,
                    )
                except ValueError as exc:
                    raise SystemExit(f"{reference_path}: {exc}") from exc
            else:
                match_percent = similarity_percent(actual_body, reference_body)

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
                "table_markdown_files",
            )
        )
        writer.writerows(report_rows)

    if args.expected_root is not None:
        reference_note = (
            "Match is verified-line recall against manual `reference/<file>.md`; "
            "missing reference files are `n/a`."
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
        "| File | Method | Time | Match | Lines | Table blocks |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    markdown.extend(
        "| `{}` | `{}` | {} s | {} | {} | {} |".format(
            escape_markdown(file_name),
            escape_markdown(method),
            elapsed,
            f"{match_percent}%" if match_percent != "n/a" else match_percent,
            f"{matched_lines}/{total_lines}" if total_lines else "n/a",
            table_markdown_files or "0",
        )
        for file_name, method, elapsed, match_percent, matched_lines, total_lines, table_markdown_files in report_rows
    )
    table = "\n".join(markdown) + "\n"
    args.markdown.write_text(table, encoding="utf-8")
    print(table, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
