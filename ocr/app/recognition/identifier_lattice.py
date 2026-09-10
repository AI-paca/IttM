from __future__ import annotations

import re
from dataclasses import dataclass

from app.chunking.vertical import TableLayout, table_words_to_rows

_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-"})
_SEGMENT = re.compile(r"^[^\s|]+$")


@dataclass(frozen=True)
class ObservedIdentifier:
    source: str
    text: str
    segments: tuple[str, ...]


@dataclass(frozen=True)
class RelationalSegmentRule:
    identifier_column: int
    identifier_segment: int
    reference_column: int
    reference_segment: int
    support_rows: int
    primary_support_rows: int


@dataclass(frozen=True)
class IdentifierFusionDecision:
    row: int
    column: int
    primary: str
    selected: str
    selected_sources: tuple[str, ...]
    rules: tuple[RelationalSegmentRule, ...]


def _identifier_token(text: str) -> str:
    tokens = [token.strip(".,;:()[]{}'\"") for token in text.translate(_DASHES).split() if token.count("-") >= 2]
    return max(tokens, key=lambda token: (token.count("-"), len(token)), default="")


def _parse_identifier(text: str) -> tuple[str, ...] | None:
    token = _identifier_token(text)
    if not token:
        return None
    segments = tuple(token.split("-"))
    if not 3 <= len(segments) <= 5:
        return None
    if any(segment and not _SEGMENT.fullmatch(segment) for segment in segments):
        return None
    if sum(bool(segment) for segment in segments) < 3:
        return None
    return segments


def _reference_segments(text: str) -> tuple[str, ...]:
    compact = text.translate(_DASHES).strip().strip(".,;:()[]{}'\"")
    if compact and " " not in compact and _SEGMENT.fullmatch(compact):
        return tuple(segment for segment in compact.split("-") if segment)
    return ()


def _cell_words(
    table: TableLayout,
    words: list[dict],
    row: int,
    column: int,
) -> list[dict]:
    left = table.x_lines[column]
    right = table.x_lines[column + 1]
    top = table.y_lines[row]
    bottom = table.y_lines[row + 1]
    return [
        word
        for word in words
        if (bbox := word.get("bbox"))
        and len(bbox) == 4
        and left <= (bbox[0] + bbox[2]) / 2 <= right
        and top <= (bbox[1] + bbox[3]) / 2 <= bottom
    ]


def _observed_by_cell(
    table: TableLayout,
    primary_words: list[dict],
    candidate_passes: tuple[tuple[str, list[dict]], ...],
) -> dict[tuple[int, int], tuple[ObservedIdentifier, ...]]:
    rows_by_source = {
        "primary": table_words_to_rows(table, primary_words),
        **{source: table_words_to_rows(table, words) for source, words in candidate_passes},
    }
    observed: dict[tuple[int, int], tuple[ObservedIdentifier, ...]] = {}
    for row in range(table.rows):
        for column in range(table.cols):
            values = []
            for source, rows in rows_by_source.items():
                text = rows[row][column].strip()
                segments = _parse_identifier(text)
                if segments is None:
                    continue
                values.append(
                    ObservedIdentifier(
                        source=source,
                        text=_identifier_token(text),
                        segments=segments,
                    )
                )
            if values:
                observed[(row, column)] = tuple(values)
    return observed


def infer_relational_identifier_rules(
    primary_rows: list[list[str]],
    observed: dict[tuple[int, int], tuple[ObservedIdentifier, ...]],
    *,
    excluded_rows: frozenset[int] = frozenset(),
) -> tuple[RelationalSegmentRule, ...]:
    rules = []
    width = max((len(row) for row in primary_rows), default=0)
    for identifier_column in range(width):
        identifier_rows = [
            row
            for row in range(len(primary_rows))
            if row not in excluded_rows
            and (candidates := observed.get((row, identifier_column)))
            and (primary := next((candidate for candidate in candidates if candidate.source == "primary"), None))
        ]
        if len(identifier_rows) < 4:
            continue
        segment_count = max(
            {
                len(
                    next(
                        candidate for candidate in observed[(row, identifier_column)] if candidate.source == "primary"
                    ).segments
                )
                for row in identifier_rows
            },
            key=lambda count: sum(
                len(
                    next(
                        candidate for candidate in observed[(row, identifier_column)] if candidate.source == "primary"
                    ).segments
                )
                == count
                for row in identifier_rows
            ),
        )
        for identifier_segment in range(segment_count):
            best = None
            for reference_column in range(width):
                if reference_column == identifier_column:
                    continue
                max_reference_segments = max(
                    (len(_reference_segments(primary_rows[row][reference_column])) for row in identifier_rows),
                    default=0,
                )
                for reference_segment in range(max_reference_segments):
                    eligible = support = primary_support = 0
                    for row in identifier_rows:
                        references = _reference_segments(primary_rows[row][reference_column])
                        candidates = observed[(row, identifier_column)]
                        primary = next(candidate for candidate in candidates if candidate.source == "primary")
                        if reference_segment >= len(references) or identifier_segment >= len(primary.segments):
                            continue
                        reference = references[reference_segment].casefold()
                        eligible += 1
                        if primary.segments[identifier_segment].casefold() == reference:
                            primary_support += 1
                        if any(
                            identifier_segment < len(candidate.segments)
                            and candidate.segments[identifier_segment].casefold() == reference
                            for candidate in candidates
                        ):
                            support += 1
                    if eligible < 4 or support < 4 or primary_support < 2 or support / eligible < 0.60:
                        continue
                    candidate_rule = RelationalSegmentRule(
                        identifier_column=identifier_column,
                        identifier_segment=identifier_segment,
                        reference_column=reference_column,
                        reference_segment=reference_segment,
                        support_rows=support,
                        primary_support_rows=primary_support,
                    )
                    rank = (support, primary_support, -reference_column, -reference_segment)
                    if best is None or rank > best[0]:
                        best = (rank, candidate_rule)
            if best is not None:
                rules.append(best[1])
    return tuple(rules)


def fuse_relational_identifier_candidates(
    table: TableLayout,
    primary_words: list[dict],
    candidate_passes: tuple[tuple[str, list[dict]], ...],
    *,
    excluded_rows: frozenset[int] = frozenset(),
) -> tuple[list[dict], tuple[IdentifierFusionDecision, ...]]:
    if not candidate_passes:
        return list(primary_words), ()
    primary_rows = table_words_to_rows(table, primary_words)
    observed = _observed_by_cell(table, primary_words, candidate_passes)
    rules = infer_relational_identifier_rules(
        primary_rows,
        observed,
        excluded_rows=excluded_rows,
    )
    rules_by_column: dict[int, list[RelationalSegmentRule]] = {}
    for rule in rules:
        rules_by_column.setdefault(rule.identifier_column, []).append(rule)

    decisions = []
    replacements = []
    replaced_cells = set()
    for (row, column), candidates in observed.items():
        if row in excluded_rows or column not in rules_by_column:
            continue
        primary = next(
            (candidate for candidate in candidates if candidate.source == "primary"),
            None,
        )
        if primary is None:
            continue
        selected_segments = list(primary.segments)
        selected_sources = ["primary" for _segment in selected_segments]
        applied_rules = []
        for rule in rules_by_column[column]:
            references = _reference_segments(primary_rows[row][rule.reference_column])
            if rule.identifier_segment >= len(selected_segments) or rule.reference_segment >= len(references):
                continue
            wanted = references[rule.reference_segment]
            if selected_segments[rule.identifier_segment] == wanted:
                continue
            alternative = next(
                (
                    candidate
                    for candidate in candidates
                    if rule.identifier_segment < len(candidate.segments)
                    and candidate.segments[rule.identifier_segment] == wanted
                ),
                None,
            )
            if alternative is None:
                alternative = next(
                    (
                        candidate
                        for candidate in candidates
                        if rule.identifier_segment < len(candidate.segments)
                        and candidate.segments[rule.identifier_segment].casefold() == wanted.casefold()
                    ),
                    None,
                )
            if alternative is None:
                continue
            selected_segments[rule.identifier_segment] = alternative.segments[rule.identifier_segment]
            selected_sources[rule.identifier_segment] = alternative.source
            applied_rules.append(rule)
        selected = "-".join(selected_segments)
        if not applied_rules or selected == primary.text:
            continue
        cell_words = _cell_words(table, primary_words, row, column)
        if not cell_words:
            continue
        bbox = (
            min(word["bbox"][0] for word in cell_words),
            min(word["bbox"][1] for word in cell_words),
            max(word["bbox"][2] for word in cell_words),
            max(word["bbox"][3] for word in cell_words),
        )
        replacements.append(
            {
                "text": selected,
                "bbox": bbox,
                "conf": max(float(word.get("conf", 0)) for word in cell_words),
                "identifier_sources": tuple(selected_sources),
            }
        )
        replaced_cells.add((row, column))
        decisions.append(
            IdentifierFusionDecision(
                row=row,
                column=column,
                primary=primary.text,
                selected=selected,
                selected_sources=tuple(selected_sources),
                rules=tuple(applied_rules),
            )
        )

    if not replacements:
        return list(primary_words), ()
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
    return [*kept, *replacements], tuple(decisions)


def words_outside_identifier_decisions(
    words: tuple[dict, ...] | list[dict],
    table: TableLayout,
    decisions: tuple[IdentifierFusionDecision, ...],
) -> list[dict]:
    cells = {(decision.row, decision.column) for decision in decisions}
    if not cells:
        return list(words)
    result = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            result.append(word)
            continue
        x = (bbox[0] + bbox[2]) / 2
        y = (bbox[1] + bbox[3]) / 2
        if any(
            table.x_lines[column] < x < table.x_lines[column + 1] and table.y_lines[row] < y < table.y_lines[row + 1]
            for row, column in cells
        ):
            continue
        result.append(word)
    return result
