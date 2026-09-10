from __future__ import annotations

from app.pipeline_core.native import native_pipeline_core


def is_isolated_heading(
    *,
    run_rows: int,
    content_chars: int,
    max_line_chars: int,
    bounded_above: bool,
    bounded_below: bool,
) -> bool:
    native = native_pipeline_core()
    if native is not None:
        return native.is_isolated_heading(
            run_rows,
            content_chars,
            max_line_chars,
            bounded_above,
            bounded_below,
        )
    return 0 < run_rows <= 2 and content_chars >= 6 and max_line_chars < 90 and bounded_above and bounded_below
