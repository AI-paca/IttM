import pytest

from tests.quality_metrics import (
    character_error_rate,
    digit_sequence_recall,
    markdown_table_shape,
    name_value_pair_recall,
    ordered_phrase_recall,
)


def test_character_error_rate_separates_quality_from_non_empty_output():
    expected = "Global Top 10 Best Performing Phones"

    assert character_error_rate(expected, expected) == 0
    assert character_error_rate(expected, "Global Top 10 Best Phones") < 0.4
    assert character_error_rate(expected, "© IUBESURCTTON 007") > 0.7


def test_digit_recall_detects_filters_that_drop_table_values():
    expected = "Alpha 1863133\nBeta 1532816\nRedmi 950814"

    assert digit_sequence_recall(expected, expected) == 1
    assert digit_sequence_recall(expected, "Alpha\nBeta\nRedmi") == 0
    assert digit_sequence_recall(expected, "1863133 1532816") == pytest.approx(2 / 3)


def test_name_value_pairs_must_survive_on_the_same_output_row():
    pairs = [
        ("Poco X7 Pro", "1863133"),
        ("Poco X6 Pro", "1532816"),
        ("Redmi Note 13 Pro+", "950814"),
    ]

    assert (
        name_value_pair_recall(
            pairs,
            "| Poco X7 Pro | 1863133 |\n" "| Poco X6 Pro | 1532816 |\n" "| Redmi Note 13 Pro+ | 950814 |",
        )
        == 1
    )
    assert name_value_pair_recall(pairs, "Poco X7 Pro\n1863133\nPoco X6 Pro") == 0


def test_reading_order_recall_penalizes_column_interleaving():
    expected = ["Heading", "first paragraph", "second paragraph", "footer"]

    assert ordered_phrase_recall(expected, "\n".join(expected)) == 1
    assert (
        ordered_phrase_recall(
            expected,
            "Heading\nsecond paragraph\nfirst paragraph\nfooter",
        )
        < 1
    )


def test_markdown_table_shape_ignores_separator_and_counts_real_cells():
    markdown = (
        "| Name | Score | Memory |\n"
        "| --- | --- | --- |\n"
        "| Poco X7 Pro | 1863133 | 12GB |\n"
        "| Poco X6 Pro | 1532816 | 12GB |"
    )

    assert markdown_table_shape(markdown) == (3, 3)
    assert markdown_table_shape("plain text") == (0, 0)
