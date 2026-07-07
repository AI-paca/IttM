from __future__ import annotations


def apply_contextual_markdown_grammar(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text

    return text
