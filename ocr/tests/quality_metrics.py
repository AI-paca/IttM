import re
from collections import Counter


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def character_error_rate(expected: str, actual: str) -> float:
    left = normalize_text(expected)
    right = normalize_text(actual)
    if not left:
        return 0.0 if not right else 1.0

    previous = list(range(len(right) + 1))
    for row, expected_char in enumerate(left, start=1):
        current = [row]
        for column, actual_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_char != actual_char),
                )
            )
        previous = current
    return previous[-1] / len(left)


def digit_sequence_recall(expected: str, actual: str) -> float:
    expected_digits = Counter(re.findall(r"\d+(?:[.,]\d+)?", expected))
    if not expected_digits:
        return 1.0
    actual_digits = Counter(re.findall(r"\d+(?:[.,]\d+)?", actual))
    matched = sum((expected_digits & actual_digits).values())
    return matched / sum(expected_digits.values())


def ordered_phrase_recall(expected_phrases: list[str], actual: str) -> float:
    normalized = normalize_text(actual)
    cursor = 0
    matched = 0
    for phrase in expected_phrases:
        position = normalized.find(normalize_text(phrase), cursor)
        if position < 0:
            continue
        matched += 1
        cursor = position + len(normalize_text(phrase))
    return matched / len(expected_phrases) if expected_phrases else 1.0


def name_value_pair_recall(expected_pairs: list[tuple[str, str]], actual: str) -> float:
    lines = [normalize_text(line) for line in actual.splitlines() if line.strip()]
    matched = 0
    for name, value in expected_pairs:
        normalized_name = normalize_text(name)
        normalized_value = normalize_text(value)
        if any(normalized_name in line and normalized_value in line for line in lines):
            matched += 1
    return matched / len(expected_pairs) if expected_pairs else 1.0


def markdown_table_shape(markdown: str) -> tuple[int, int]:
    rows = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return len(rows), max((len(row) for row in rows), default=0)
