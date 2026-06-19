from collections.abc import Callable

from app.chunking.vertical import (
    TableLayout,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)

TableWordFormatter = Callable[[TableLayout, list[dict]], str]

TABLE_WORD_FORMATTERS: dict[str, TableWordFormatter] = {
    "generic_markdown": table_words_to_markdown,
    "curriculum": wide_curriculum_table_to_markdown,
}


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
