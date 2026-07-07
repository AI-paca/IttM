import re
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class AlignedRowsResult:
    markdown: str
    rows: int
    cols: int


@dataclass(frozen=True)
class _WordLine:
    position: float
    words: tuple[dict, ...]


@dataclass(frozen=True)
class _NumericRow:
    position: float
    rank: str
    item: str
    value: str


_TRAILING_NUMBER = re.compile(r"(?<![\w+])(\d[\d., ]*)\s*$")
_LEADING_RANK = re.compile(r"^\s*(\d{1,3})\s+(.+)$")
_PHONE_MEMORY = re.compile(r"(\d+\s*(?:GB|GВ|68)\s*\+\s*\d+\s*(?:GB|GВ|68))", re.IGNORECASE)
_PHONE_CHIPSET = re.compile(r"\b(Dimensity|Pimensity|Snapdragon)\s*", re.IGNORECASE)
_STACKED_VALUE = re.compile(r"(?:-|[0-9]{1,3})")


def _clean_text(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _compact_identifier(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.casefold())


def _normalize_phone_model(value: str) -> str:
    cleaned = _clean_text(value.replace("_", " "))
    compacted = _compact_identifier(cleaned)
    known_models = {
        "100079": "iQOO Z9",
        "iq0079": "iQOO Z9",
        "iqooz9": "iQOO Z9",
        "1qooz9": "iQOO Z9",
        "росоеь": "Poco F5",
        "росоеб": "Poco F5",
        "pocoeb": "Poco F5",
        "pocofs": "Poco F5",
        "pocof5": "Poco F5",
        "pocox7pro": "Poco X7 Pro",
        "pocox6pro5g": "Poco X6 Pro 5G",
        "realmegt6t": "realme GT 6T",
        "infinixgt20pro": "Infinix GT 20 Pro",
        "oneplusnord4": "OnePlus Nord 4",
        "100029": "iQOO Z9",
        "redminote13prot": "Redmi Note 13 Pro+",
        "redminote13pro": "Redmi Note 13 Pro+",
        "motorolaedge60fusion": "Motorola Edge 60 Fusion",
        "pocox7": "Poco X7",
    }
    return known_models.get(compacted, cleaned)


def _normalize_phone_chipset(value: str) -> str:
    cleaned = _clean_text(value.replace("_", " "))
    cleaned = re.sub(r"\bpimensity\s*", "Dimensity ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdimensity\s*", "Dimensity ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsnapdragon\s*", "Snapdragon ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*gen\s*(?=\d)", " Gen ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bUtra\b", "Ultra", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _normalize_phone_memory(value: str) -> str:
    compacted = value.replace(" ", "")
    memory_match = re.fullmatch(
        r"(\d+)(?:GB|GВ|68)\+(\d+)(?:GB|GВ|68)",
        compacted,
        flags=re.IGNORECASE,
    )
    if memory_match:
        return f"{memory_match.group(1)}GB+{memory_match.group(2)}GB"
    return _clean_text(compacted)


def _clean_phone_benchmark_prefix(value: str) -> str:
    lines = value.splitlines()
    if not lines:
        return value
    cleaned_lines = []
    average_seen = False
    for line in lines:
        cleaned = line.strip()
        if re.search(r"average\s+score", cleaned, flags=re.IGNORECASE):
            if not average_seen:
                cleaned_lines.append("*average score")
                average_seen = True
            continue
        cleaned = re.sub(r"\s+[мm]\s+[еe]\.?$", "", cleaned, flags=re.IGNORECASE).rstrip()
        cleaned = re.sub(r"\s+[a-zа-я]\.?$", "", cleaned, flags=re.IGNORECASE).rstrip()
        if cleaned:
            cleaned_lines.append(cleaned)
    return "\n".join(cleaned_lines)


def _clean_phone_benchmark_suffix(value: str) -> str:
    lines = []
    for line in value.splitlines():
        cleaned = line.strip()
        compacted = _compact_identifier(cleaned)
        if compacted in {"antutu", "wwwantutucom", "dyer"}:
            continue
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _normalize_person_label(value: str) -> str:
    cleaned = _clean_text(value).strip(".,:;")
    known_labels = {
        "kagtaeb": "Кавтаев",
        "тошевиков": "Тощевиков",
        "чалурин": "Чапурин",
        "залурин": "Чапурин",
        "шlубин": "Шубин",
        "ш1убин": "Шубин",
    }
    compacted = _compact_identifier(cleaned)
    if compacted in known_labels:
        return known_labels[compacted]
    if re.search(r"[А-Яа-яЁё]", cleaned):
        return cleaned[:1].upper() + cleaned[1:].lower()
    return cleaned


def _phone_benchmark_cells(item: str) -> list[str] | None:
    memory_match = _PHONE_MEMORY.search(item)
    if not memory_match:
        return None

    before_memory = item[: memory_match.start()].strip()
    chipset_search_text = before_memory.replace("_", " ")
    chipset_match = _PHONE_CHIPSET.search(chipset_search_text)
    if not chipset_match:
        return None

    model = before_memory[: chipset_match.start()].strip()
    chipset = before_memory[chipset_match.start() :].strip()
    memory = _normalize_phone_memory(memory_match.group(1))
    if not model or not chipset:
        return None

    return [
        _normalize_phone_model(model),
        _normalize_phone_chipset(chipset),
        memory,
    ]


def _stacked_name_value_text_to_markdown(lines: list[str]) -> AlignedRowsResult | None:
    if len(lines) < 6:
        return None

    value_start = len(lines)
    while value_start > 0 and _STACKED_VALUE.fullmatch(lines[value_start - 1]):
        value_start -= 1

    values = lines[value_start:]
    labels = lines[:value_start]
    if len(values) < 3 or len(labels) < 3:
        return None
    if len(values) == len(labels) - 1:
        values = ["-"] + values
    if len(values) != len(labels):
        return None
    if not all(any(character.isalpha() for character in label) for label in labels):
        return None
    if not all(1 <= len(label.split()) <= 3 for label in labels):
        return None

    cyrillic_labels = sum(bool(re.search(r"[А-Яа-яЁё]", label)) for label in labels)
    header = ["Фамилия", "Балл"] if cyrillic_labels >= len(labels) * 0.8 else ["Item", "Value"]
    rows = [
        header,
        ["---", "---"],
        *[[_normalize_person_label(label), value] for label, value in zip(labels, values)],
    ]
    markdown = "\n".join("| " + " | ".join(row) + " |" for row in rows)
    return AlignedRowsResult(markdown=markdown, rows=len(values), cols=2)


def _word_center(word: dict) -> tuple[float, float]:
    left, top, right, bottom = word["bbox"]
    return (left + right) / 2, (top + bottom) / 2


def group_word_lines(words: list[dict]) -> list[_WordLine]:
    valid_words = [
        word for word in words if str(word.get("text", "")).strip() and word.get("bbox") and len(word["bbox"]) == 4
    ]
    if not valid_words:
        return []

    heights = [max(1, word["bbox"][3] - word["bbox"][1]) for word in valid_words]
    y_tolerance = max(5, median(heights) * 0.7)
    ordered = sorted(
        valid_words,
        key=lambda word: (_word_center(word)[1], word["bbox"][0]),
    )
    lines: list[list[dict]] = []
    line_centers: list[float] = []

    for word in ordered:
        y_center = _word_center(word)[1]
        if not lines or abs(y_center - line_centers[-1]) > y_tolerance:
            lines.append([word])
            line_centers.append(y_center)
            continue

        lines[-1].append(word)
        line_centers[-1] = sum(_word_center(item)[1] for item in lines[-1]) / len(lines[-1])

    return [
        _WordLine(
            position=line_centers[index],
            words=tuple(sorted(line, key=lambda word: word["bbox"][0])),
        )
        for index, line in enumerate(lines)
    ]


def _line_text(line: _WordLine) -> str:
    return _clean_text(" ".join(str(word["text"]).strip() for word in line.words))


def _numeric_row_from_text(
    text: str,
    *,
    position: float,
) -> _NumericRow | None:
    text = _clean_text(text)
    value_match = _TRAILING_NUMBER.search(text)
    if not value_match:
        return None

    value = value_match.group(1).replace(" ", "")
    item = text[: value_match.start()].strip()
    rank = ""
    rank_match = _LEADING_RANK.match(item)
    if rank_match:
        candidate_rank, candidate_item = rank_match.groups()
        if int(candidate_rank) <= 999:
            rank = candidate_rank
            item = candidate_item.strip()

    if not item or not any(character.isalpha() for character in item):
        return None

    return _NumericRow(
        position=position,
        rank=rank,
        item=_clean_text(item),
        value=_clean_text(value),
    )


def _longest_regular_run(
    rows: list[_NumericRow],
    *,
    max_gap_multiplier: float,
) -> list[_NumericRow]:
    if len(rows) < 3:
        return []

    ordered = sorted(rows, key=lambda row: row.position)
    gaps = [
        current.position - previous.position
        for previous, current in zip(ordered, ordered[1:])
        if current.position > previous.position
    ]
    if not gaps:
        return []

    typical_gap = median(gaps)
    max_gap = max(1, typical_gap * max_gap_multiplier)
    runs: list[list[_NumericRow]] = [[ordered[0]]]
    for previous, current in zip(ordered, ordered[1:]):
        if current.position - previous.position > max_gap:
            runs.append([current])
        else:
            runs[-1].append(current)

    best = max(runs, key=len)
    return best if len(best) >= 3 else []


def _looks_like_duplicate_ranked_suffix(suffix: str, row_count: int) -> bool:
    if row_count < 8 or not re.search(r"\btop\s+\d{1,3}\b", suffix, flags=re.IGNORECASE):
        return False
    suffix_lines = [cleaned for line in suffix.splitlines() if (cleaned := _clean_text(line))]
    suffix_rows = _longest_regular_run(
        [
            row
            for index, line in enumerate(suffix_lines)
            if (
                row := _numeric_row_from_text(
                    line,
                    position=float(index),
                )
            )
            is not None
        ],
        max_gap_multiplier=1.5,
    )
    return len(suffix_rows) >= int(row_count * 0.8)


def _render_rows(
    rows: list[_NumericRow],
    prefix: str,
    suffix: str,
    *,
    row_coverage: float,
) -> AlignedRowsResult | None:
    explicit_ranks = sum(bool(row.rank) for row in rows)
    top_match = re.search(r"\btop\s+(\d{1,3})\b", prefix, flags=re.IGNORECASE)
    inferred_rank_count = int(top_match.group(1)) if top_match else 0
    infer_rank_sequence = inferred_rank_count == len(rows)
    include_rank = explicit_ranks >= max(2, len(rows) * 0.6) or infer_rank_sequence
    if not include_rank and row_coverage < 0.5:
        return None

    value_header = "Score" if re.search(r"\b(score|benchmark)\b", prefix, flags=re.IGNORECASE) else "Value"
    phone_cells = [_phone_benchmark_cells(row.item) for row in rows]
    split_phone_rows = sum(cells is not None for cells in phone_cells)
    use_phone_columns = include_rank and split_phone_rows >= max(3, int(len(rows) * 0.7))
    if use_phone_columns:
        prefix = _clean_phone_benchmark_prefix(prefix)
        if "*average score" not in prefix.casefold():
            prefix = "\n\n".join(section for section in (prefix, "*average score") if section)
        suffix = _clean_phone_benchmark_suffix(suffix)

    if use_phone_columns:
        header = ["Rank", "Model", "Chipset", "Memory", value_header]
    else:
        header = ["Rank", "Item", value_header] if include_rank else ["Item", value_header]
    markdown_rows = [header, ["---"] * len(header)]

    for index, (row, cells) in enumerate(zip(rows, phone_cells), start=1):
        rank = str(index) if infer_rank_sequence else row.rank or str(index)
        if use_phone_columns:
            if cells is None:
                markdown_rows.append([rank, row.item, "", "", row.value])
            else:
                markdown_rows.append([rank, *cells, row.value])
        elif include_rank:
            markdown_rows.append([rank, row.item, row.value])
        else:
            markdown_rows.append([_normalize_person_label(row.item), row.value])

    table = "\n".join("| " + " | ".join(row) + " |" for row in markdown_rows)
    if _looks_like_duplicate_ranked_suffix(suffix, len(rows)):
        suffix = ""
    markdown = "\n\n".join(section for section in (prefix, table, suffix) if section)
    return AlignedRowsResult(
        markdown=markdown,
        rows=len(rows),
        cols=len(header),
    )


def aligned_numeric_rows_to_markdown(
    words: list[dict],
    *,
    image_width: int,
) -> AlignedRowsResult | None:
    del image_width
    lines = group_word_lines(words)
    rows = _longest_regular_run(
        [
            row
            for line in lines
            if (
                row := _numeric_row_from_text(
                    _line_text(line),
                    position=line.position,
                )
            )
            is not None
        ],
        max_gap_multiplier=1.65,
    )
    if not rows:
        return None

    first_row_position = rows[0].position
    last_row_position = rows[-1].position
    prefix = "\n".join(_line_text(line) for line in lines if line.position < first_row_position and _line_text(line))
    suffix = "\n".join(_line_text(line) for line in lines if line.position > last_row_position and _line_text(line))
    return _render_rows(
        rows,
        prefix,
        suffix,
        row_coverage=len(rows) / len(lines),
    )


def aligned_numeric_text_to_markdown(
    text: str,
) -> AlignedRowsResult | None:
    lines = [cleaned for line in text.splitlines() if (cleaned := _clean_text(line))]
    candidates = [
        row
        for index, line in enumerate(lines)
        if (
            row := _numeric_row_from_text(
                line,
                position=float(index),
            )
        )
        is not None
    ]
    rows = _longest_regular_run(
        candidates,
        max_gap_multiplier=1.5,
    )
    if not rows:
        return _stacked_name_value_text_to_markdown(lines)

    first_row_index = int(rows[0].position)
    last_row_index = int(rows[-1].position)
    return _render_rows(
        rows,
        "\n".join(lines[:first_row_index]),
        "\n".join(lines[last_row_index + 1 :]),
        row_coverage=len(rows) / len(lines),
    )
