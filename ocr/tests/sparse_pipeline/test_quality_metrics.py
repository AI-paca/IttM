from __future__ import annotations

import itertools

import pytest

from app.sparse_pipeline.quality_metrics import (
    exact_levenshtein,
    exact_text_metric,
)


def _quadratic_oracle(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + int(left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def test_bit_vector_distance_matches_quadratic_oracle_for_short_unicode() -> None:
    values = tuple(
        "".join(characters)
        for length in range(5)
        for characters in itertools.product("a中", repeat=length)
    )

    for left in values:
        for right in values:
            assert exact_levenshtein(left, right) == _quadratic_oracle(left, right)


def test_long_document_alignment_is_exact_without_quadratic_cell_rejection() -> None:
    # 13,561 compact codepoints is the size of the 000041 manual reference.
    # The former 4M-cell DP rejected any useful OCR output beyond 293 chars.
    reference = "А" * 13_561
    recognized = "Б" * 13_561

    assert exact_text_metric(
        reference,
        recognized,
        max_cells=4_000_000,
    ) == (
        13_561,
        13_561,
        13_561,
        0.0,
    )


def test_common_document_edges_are_removed_before_work_budgeting() -> None:
    prefix = "общий префикс" * 2_000
    suffix = "общий суффикс" * 2_000

    assert exact_levenshtein(
        prefix + "x" + suffix,
        prefix + "y" + suffix,
        max_cells=1,
    ) == 1


def test_bit_vector_work_limit_still_fails_closed() -> None:
    # 4,096 pattern bits occupy 64 logical 64-bit words; 4,096 updates need
    # 262,144 bounded work units.
    with pytest.raises(ValueError, match="bit-vector work limit"):
        exact_levenshtein(
            "a" * 4_096,
            "b" * 4_096,
            max_cells=262_143,
        )
