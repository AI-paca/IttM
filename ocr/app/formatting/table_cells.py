from __future__ import annotations

import re

_NUMERIC_RATIO_CELL = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def format_markdown_table_cell(value: str) -> str:
    """Canonicalize only a complete numeric-ratio table cell."""

    match = _NUMERIC_RATIO_CELL.fullmatch(value)
    if match is None:
        return value
    return f"{match.group(1)} / {match.group(2)}"


__all__ = ["format_markdown_table_cell"]
