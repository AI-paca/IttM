from collections.abc import Callable
from dataclasses import dataclass

from app.chunking.vertical import (
    TableLayout,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)

TableWordFormatter = Callable[[TableLayout, list[dict]], str]


@dataclass(frozen=True)
class TableProcessingPlan:
    layout_normalization: str
    word_recognition: str
    formatter_names: tuple[str, ...]
    reason: str = ""


TABLE_WORD_FORMATTERS: dict[str, TableWordFormatter] = {
    "generic_markdown": table_words_to_markdown,
    "curriculum": wide_curriculum_table_to_markdown,
}


def select_table_processing_plan(
    table: TableLayout,
    *,
    layout_normalization: str,
    word_recognition: str,
    formatter_names: tuple[str, ...],
) -> TableProcessingPlan:
    if table.cols >= 30 and table.rows >= 5:
        return TableProcessingPlan(
            layout_normalization="preserve_grid",
            word_recognition="single_pass_with_left_strip",
            formatter_names=("curriculum", "generic_markdown"),
            reason="wide_curriculum_grid",
        )
    return TableProcessingPlan(
        layout_normalization=layout_normalization,
        word_recognition=word_recognition,
        formatter_names=formatter_names,
    )


def format_table_words(
    formatter_name: str,
    table: TableLayout,
    words: list[dict],
) -> str:
    formatter = TABLE_WORD_FORMATTERS.get(formatter_name)
    if formatter is None:
        known = ", ".join(sorted(TABLE_WORD_FORMATTERS))
        raise ValueError(f"Unknown table formatter '{formatter_name}'. Known formatters: {known}")
    return formatter(table, words)
