#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
REPORT_PATH = REPO_ROOT / "scripts" / "debug" / "debug_report.py"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.formatting.structural_grammar import (
    SparseMarkdownRow,
    render_sparse_markdown_rows,
)
from app.layout.contracts import (
    LayoutDecision,
    LayoutFeatures,
    LayoutStageSpec,
)
from app.layout.recursive_grid import (
    RecursiveGridConfig,
    project_sparse_shadow,
    segment_recursive_grid,
)
from app.layout.stages import execute_layout_decision
from app.preprocessing import OcrPreprocessingPipeline
from app.services.convert_service import (
    _looks_like_dark_ui_text_page,
    _sparse_rows_have_confirmed_structure,
)

PREPROCESSING = (
    "projector_slide_dewarp",
    "mobile_screen_upscale",
    "small_text_upscale",
    "projected_document_dewarp",
)


@dataclass(frozen=True)
class Case:
    image: Path
    reference: Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the current sparse 3/5 grammar with Markdown "
            "references without running OCR."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        required=True,
        metavar="IMAGE:REFERENCE",
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dump-matrix",
        action="store_true",
        help="Print direct recursive-grid raw and derived sparse matrices.",
    )
    parser.add_argument("--matrix-max-rows", type=int, default=60)
    parser.add_argument("--matrix-max-cols", type=int, default=40)
    return parser.parse_args()


def _case(value: str) -> Case:
    image, separator, reference = value.partition(":")
    if not separator:
        raise ValueError("Case must be IMAGE:REFERENCE")
    return Case(Path(image), Path(reference))


def _report_module():
    spec = importlib.util.spec_from_file_location(
        "ittm_debug_report",
        REPORT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {REPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision() -> LayoutDecision:
    return LayoutDecision(
        label="sparse-grammar-check",
        stages=(
            LayoutStageSpec(
                name="recursive_grid",
                parameters=(
                    ("max_region_height", 1400),
                    ("min_region_height", 120),
                    ("min_region_width", 80),
                    ("min_separator_gap", 8),
                    ("max_depth", 32),
                ),
            ),
        ),
        confidence=1.0,
    )


def _placeholder_table(rows: int, cols: int) -> str:
    rows = max(1, rows)
    cols = max(1, cols)
    header = "| " + " | ".join("x" for _ in range(cols)) + " |"
    separator = "| " + " | ".join("---" for _ in range(cols)) + " |"
    body = [
        "| " + " | ".join("x" for _ in range(cols)) + " |"
        for _ in range(rows - 1)
    ]
    return "\n".join((header, separator, *body))


def _content_left(metadata: dict[str, object]) -> int | None:
    bbox = metadata.get("content_bbox")
    if (
        isinstance(bbox, (tuple, list))
        and len(bbox) == 4
        and isinstance(bbox[0], int)
    ):
        return bbox[0]
    return None


def _actual_markdown(image: Image.Image) -> tuple[str, tuple[int, ...]]:
    regions = execute_layout_decision(
        image,
        LayoutFeatures(
            width=image.width,
            height=image.height,
            foreground_ratio=0.0,
        ),
        _decision(),
        min_confirmed_cell_ratio=0.0,
    )
    sparse_rows = []
    sparse_top: int | None = None
    table_chunks: list[tuple[int, str]] = []
    raw_codes = []
    seen_markdown = False
    page_table_confirmed = (
        any(region.table is not None for region in regions)
        and not _looks_like_dark_ui_text_page(image)
    )

    def flush_sparse_rows() -> None:
        nonlocal sparse_rows, sparse_top, seen_markdown
        if not sparse_rows:
            return
        if (
            not page_table_confirmed
            and not _sparse_rows_have_confirmed_structure(
                sparse_rows,
            )
        ):
            table_chunks.append(
                (
                    sparse_top
                    if sparse_top is not None
                    else image.height,
                    "\n".join("x" for _ in sparse_rows),
                )
            )
            sparse_rows = []
            sparse_top = None
            return
        rendered = render_sparse_markdown_rows(
            sparse_rows,
            first_heading_level=2 if seen_markdown else 1,
        )
        if rendered.markdown:
            table_chunks.append(
                (
                    sparse_top if sparse_top is not None else image.height,
                    rendered.markdown,
                )
            )
            seen_markdown = True
        sparse_rows = []
        sparse_top = None

    try:
        for region in regions:
            if region.table is not None:
                flush_sparse_rows()
                table_chunks.append(
                    (
                        region.bbox[1],
                        _placeholder_table(
                            region.table.rows,
                            region.table.cols,
                        ),
                    )
                )
                seen_markdown = True
                continue
            metadata = region.metadata or {}
            if metadata.get("structural_only") is True:
                continue
            anchor = metadata.get("grid_row"), metadata.get("grid_col")
            codes = metadata.get("sparse_codes")
            if not (
                all(isinstance(value, int) for value in anchor)
                and isinstance(codes, (tuple, list))
            ):
                continue
            normalized_codes = tuple(
                tuple(code)
                for code in codes
                if (
                    isinstance(code, (tuple, list))
                    and len(code) == 3
                    and all(isinstance(value, int) for value in code)
                )
            )
            raw_codes.extend(code[2] for code in normalized_codes)
            sparse_rows.append(
                SparseMarkdownRow(
                    parts=("x",),
                    anchor=anchor,
                    codes=normalized_codes,
                    list_marker=bool(metadata.get("list_marker")),
                    content_left=_content_left(metadata),
                )
            )
            sparse_top = (
                region.bbox[1]
                if sparse_top is None
                else min(sparse_top, region.bbox[1])
            )

        flush_sparse_rows()
        return (
            "\n\n".join(
                markdown
                for _, markdown in sorted(table_chunks)
                if markdown
            ),
            tuple(raw_codes),
        )
    finally:
        for region in regions:
            if region.image is not image:
                region.image.close()


def _format_matrix(
    matrix: list[list[int]],
    *,
    max_rows: int,
    max_cols: int,
) -> str:
    if not matrix:
        return "(empty)"
    row_limit = max(1, max_rows)
    col_limit = max(1, max_cols)
    rows = matrix[:row_limit]
    cols = list(range(min(len(matrix[0]), col_limit)))
    lines = ["cols: " + " ".join(f"{column:>2}" for column in cols)]
    for row_number, row in enumerate(rows):
        lines.append(
            f"{row_number:>3}: "
            + " ".join(f"{row[column]:>2}" for column in cols)
            + (" ..." if len(row) > col_limit else "")
        )
    if len(matrix) > row_limit:
        lines.append(f"... rows truncated {len(matrix) - row_limit}")
    return "\n".join(lines)


def _derived_sparse_matrix(raw: list[list[int]]) -> list[list[int]]:
    derived = [row[:] for row in raw]
    for row_number, row in enumerate(raw):
        nonzero_columns = [
            column
            for column, value in enumerate(row)
            if value != 0
        ]
        for run in _consecutive_runs(nonzero_columns):
            if len(run) >= 4:
                for column in run:
                    derived[row_number][column] = 7
        merge_left_columns = [
            column
            for column, value in enumerate(row)
            if value in {5, 8}
        ]
        if len(merge_left_columns) >= 3:
            for column, value in enumerate(row):
                if value != 0:
                    derived[row_number][column] = 7
        for run in _consecutive_runs(merge_left_columns):
            if len(run) >= 4:
                for column in run:
                    derived[row_number][column] = 7

    eight_columns_by_row = {
        row_number: {
            column
            for column, value in enumerate(row)
            if value == 8
        }
        for row_number, row in enumerate(raw)
    }
    row_numbers = sorted(
        row_number
        for row_number, columns in eight_columns_by_row.items()
        if columns
    )
    for first_index, first_row in enumerate(row_numbers):
        for second_row in row_numbers[first_index + 1:]:
            common = (
                eight_columns_by_row[first_row]
                & eight_columns_by_row[second_row]
            )
            for run in _consecutive_runs(sorted(common)):
                if len(run) < 2:
                    continue
                for row_number in range(first_row, second_row + 1):
                    for column in run:
                        if (
                            row_number < len(derived)
                            and column < len(derived[row_number])
                            and raw[row_number][column] != 0
                        ):
                            derived[row_number][column] = 7
    return derived


def _consecutive_runs(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    runs: list[list[int]] = [[values[0]]]
    for value in values[1:]:
        if value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def _direct_sparse_matrix_dump(
    image: Image.Image,
    *,
    max_rows: int,
    max_cols: int,
) -> str:
    leaves = segment_recursive_grid(
        image,
        RecursiveGridConfig(
            max_depth=32,
            max_region_height=1400,
            min_cell_height=24,
            min_separator_gap=8,
            overlap=16,
            deskew=True,
            preprocess_steps=("recursive_page_dewarp",),
        ),
    )
    try:
        projection = project_sparse_shadow(leaves)
        if projection.rows <= 0 or projection.cols <= 0:
            return "direct_sparse rows=0 cols=0 leaves=0\nraw:\n(empty)"
        raw = [
            [0 for _ in range(projection.cols)]
            for _ in range(projection.rows)
        ]
        for _, anchor, _ in projection.leaf_projection:
            raw[anchor[0]][anchor[1]] = max(raw[anchor[0]][anchor[1]], 1)
        for row, column, code in projection.codes:
            raw[row][column] = max(raw[row][column], code)
        derived = _derived_sparse_matrix(raw)
        return (
            f"direct_sparse rows={projection.rows} cols={projection.cols} "
            f"leaves={len(leaves)} codes={len(projection.codes)}\n"
            "raw:\n"
            + _format_matrix(raw, max_rows=max_rows, max_cols=max_cols)
            + "\n"
            "derived:\n"
            + _format_matrix(
                derived,
                max_rows=max_rows,
                max_cols=max_cols,
            )
        )
    finally:
        for leaf in leaves:
            leaf.image.close()


def _strip_page_wrapper(markdown: str) -> str:
    return re.sub(
        r"\A\s*##\s+Page\s+\d+\s*\n+",
        "",
        markdown,
        count=1,
        flags=re.IGNORECASE,
    )


def _check(
    case: Case,
    report,
    *,
    dump_matrix: bool,
    matrix_max_rows: int,
    matrix_max_cols: int,
) -> dict[str, object]:
    with Image.open(case.image) as opened:
        source = opened.convert("RGB")
    pipeline = OcrPreprocessingPipeline.from_step_names(PREPROCESSING)
    image = pipeline.apply(source)
    if image is not source:
        source.close()
    try:
        actual, raw_codes = _actual_markdown(image)
        matrix_dump = (
            _direct_sparse_matrix_dump(
                image,
                max_rows=matrix_max_rows,
                max_cols=matrix_max_cols,
            )
            if dump_matrix
            else ""
        )
    finally:
        image.close()

    expected = _strip_page_wrapper(
        case.reference.read_text(
            encoding="utf-8",
            errors="replace",
        )
    )
    actual_lint = report.lint_markdown_controls(actual)
    reference_lint = report.lint_markdown_controls(expected)
    infer_reference_controls = report.should_infer_plain_reference_controls(
        expected,
    )
    actual_context = report.markdown_context_shadow_codes(actual)
    expected_context = report.markdown_context_shadow_codes(
        expected,
        infer_plain_headings=infer_reference_controls,
    )
    score = report.sequence_token_score(
        actual_context,
        expected_context,
    )
    notes = (
        f"context={len(actual_context)}/{len(expected_context)};"
        f"{score:.2f}; "
        f"lint={'pass' if not actual_lint else 'fail'}"
    )
    passed = (
        score == 100.0
        and not actual_lint
        and not reference_lint
    )
    return {
        "file": case.image.name,
        "grammar_percent": f"{score:.2f}",
        "gate": "pass" if passed else "fail",
        "raw_codes": ",".join(str(code) for code in raw_codes) or "none",
        "actual_shadow": ",".join(
            str(code)
            for code in actual_context
        )
        or "none",
        "expected_shadow": ",".join(
            str(code)
            for code in expected_context
        )
        or "none",
        "actual_lint": "; ".join(actual_lint) or "pass",
        "reference_lint": "; ".join(reference_lint) or "pass",
        "notes": notes,
        "matrix_dump": matrix_dump,
    }


def main() -> int:
    args = _parse_args()
    report = _report_module()
    cases = [_case(value) for value in args.case]
    with ThreadPoolExecutor(
        max_workers=max(1, args.workers)
    ) as executor:
        rows = list(
            executor.map(
                lambda case: _check(
                    case,
                    report,
                    dump_matrix=args.dump_matrix,
                    matrix_max_rows=args.matrix_max_rows,
                    matrix_max_cols=args.matrix_max_cols,
                ),
                cases,
            )
        )

    for row in rows:
        print(
            f"{row['grammar_percent']:>14} {row['gate']:<4} "
            f"raw_sparse=[{row['raw_codes']}] "
            f"grammar_actual=[{row['actual_shadow']}] "
            f"grammar_expected=[{row['expected_shadow']}] "
            f"{row['file']}"
        )
        if args.dump_matrix:
            print(row["matrix_dump"])
    passed = sum(row["gate"] == "pass" for row in rows)
    applicable = sum(row["gate"] != "n/a" for row in rows)
    print(f"\nPass: {passed}/{applicable}")

    if args.output is not None and rows:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as stream:
            fieldnames = tuple(
                key
                for key in rows[0]
                if key != "matrix_dump" or args.dump_matrix
            )
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(
                {
                    key: value
                    for key, value in row.items()
                    if key in fieldnames
                }
                for row in rows
            )
        print(f"Wrote {args.output}")
    return 1 if any(row["gate"] == "fail" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
