from app.chunking.vertical import TableCell, TableLayout
from app.recognition.identifier_lattice import (
    fuse_relational_identifier_candidates,
    infer_relational_identifier_rules,
    words_outside_identifier_decisions,
)


def _table(rows: int = 6) -> TableLayout:
    return TableLayout(
        bbox=(0, 0, 400, rows * 40),
        rows=rows,
        cols=2,
        x_lines=(0, 220, 400),
        y_lines=tuple(range(0, (rows + 1) * 40, 40)),
        cells=tuple(
            TableCell(
                row=row,
                col=column,
                bbox=(column * 220, row * 40, 220 if column == 0 else 400, (row + 1) * 40),
            )
            for row in range(rows)
            for column in range(2)
        ),
    )


def _words(values: list[tuple[str, str]]) -> list[dict]:
    words = []
    for row, (identifier, reference) in enumerate(values):
        words.extend(
            (
                {
                    "text": identifier,
                    "bbox": (10, row * 40 + 10, 190, row * 40 + 30),
                    "conf": 70,
                },
                {
                    "text": reference,
                    "bbox": (230, row * 40 + 10, 360, row * 40 + 30),
                    "conf": 80,
                },
            )
        )
    return words


def test_relational_rule_replaces_only_observed_same_row_segments():
    table = _table()
    primary_values = [
        ("й-A1-X-001", "A1-R"),
        ("й-B2-X-002", "B2-R"),
        ("й-C3-X-003", "C3-R"),
        ("u-D4-X-004", "D4-R"),
        ("й-67-X-007", "G7-R"),
        ("n-/6-X-006", "F6-R"),
    ]
    observed_values = [
        ("й-A1-X-001", "A1-R"),
        ("й-B2-X-002", "B2-R"),
        ("й-C3-X-003", "C3-R"),
        ("й-D4-X-004", "D4-R"),
        ("й-G7-X-007", "G7-R"),
        ("n-F6-X-006", "F6-R"),
    ]

    fused, decisions = fuse_relational_identifier_candidates(
        table,
        _words(primary_values),
        (("alternate", _words(observed_values)),),
    )

    assert [(decision.row, decision.primary, decision.selected) for decision in decisions] == [
        (4, "й-67-X-007", "й-G7-X-007"),
        (5, "n-/6-X-006", "n-F6-X-006"),
    ]
    # The row-3 prefix has no relational reference and therefore remains the
    # directly observed primary 'u', even though another row uses 'й'.
    row_text = {
        row: next(
            word["text"]
            for word in fused
            if word["bbox"][1] < (row * 40 + 20) < word["bbox"][3] and word["bbox"][0] < 220
        )
        for row in range(6)
    }
    assert row_text[3] == "u-D4-X-004"
    assert row_text[4] == "й-G7-X-007"
    assert row_text[5] == "n-F6-X-006"


def test_relation_requires_four_supported_rows_and_two_primary_matches():
    primary_rows = [
        ["й-A1-X-001", "A1-R"],
        ["й-67-X-007", "G7-R"],
        ["й-Н8-X-008", "H8-R"],
    ]

    assert infer_relational_identifier_rules(primary_rows, {}) == ()


def test_micro_words_are_filtered_only_for_replaced_identifier_cells():
    table = _table()
    primary = _words(
        [
            ("й-A1-X-001", "A1-R"),
            ("й-B2-X-002", "B2-R"),
            ("й-C3-X-003", "C3-R"),
            ("й-D4-X-004", "D4-R"),
            ("й-67-X-007", "G7-R"),
            ("й-F6-X-006", "F6-R"),
        ]
    )
    observed = _words(
        [
            ("й-A1-X-001", "A1-R"),
            ("й-B2-X-002", "B2-R"),
            ("й-C3-X-003", "C3-R"),
            ("й-D4-X-004", "D4-R"),
            ("й-G7-X-007", "G7-R"),
            ("й-F6-X-006", "F6-R"),
        ]
    )
    _fused, decisions = fuse_relational_identifier_candidates(
        table,
        primary,
        (("alternate", observed),),
    )
    recovered = [
        {"text": "duplicate", "bbox": (10, 170, 190, 190)},
        {"text": "other cell", "bbox": (230, 170, 360, 190)},
        {"text": "other row", "bbox": (10, 210, 190, 230)},
    ]

    assert [
        word["text"]
        for word in words_outside_identifier_decisions(
            recovered,
            table,
            decisions,
        )
    ] == ["other cell", "other row"]
