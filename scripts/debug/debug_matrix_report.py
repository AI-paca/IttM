#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_config import OCR_PIPELINE_PROFILES  # noqa: E402
from app.pipeline_flags import profile_flags  # noqa: E402
from scripts.debug.debug_report import expected_match, result_body  # noqa: E402

DEFAULT_THRESHOLD = 87.0
PDF_THRESHOLD = 90.0
THRESHOLD_OVERRIDES = {
    "Adobe Scan Oct 26, 2022 (1).pdf": 70.0,
    "photo_6_2026-05-12_22-26-36.jpg": 50.0,
}

BROWSER_TESSERACT_METHOD = "browser-tesseract"
DEFAULT_BROWSER_TESSERACT_PROFILE = "browser_tesseract_dewarp"


def _float_or_none(value: str) -> float | None:
    if value in {"", "n/a"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def _flags_for_profile(method: str, profile_name: str) -> set[str]:
    if method == BROWSER_TESSERACT_METHOD:
        return set()
    profile = OCR_PIPELINE_PROFILES.get(profile_name)
    return profile_flags(profile) if profile else set()


def _format_flags(
    method: str,
    profile_name: str,
    summary_row: dict[str, str] | None = None,
) -> str:
    if summary_row and summary_row.get("flags"):
        return summary_row["flags"]
    flags = _flags_for_profile(method, profile_name)
    return "; ".join(sorted(flags))


def _threshold_for_file(file_name: str) -> float:
    match = re.fullmatch(
        r"(.+\.pdf)(?:\.page-\d{3})?\.raster\.(?:png|jpg)", file_name, re.I
    )
    if match:
        file_name = match.group(1)
    if file_name in THRESHOLD_OVERRIDES:
        return THRESHOLD_OVERRIDES[file_name]
    if file_name.casefold().endswith(".pdf"):
        return PDF_THRESHOLD
    return DEFAULT_THRESHOLD


def _gate(value: str, threshold: float) -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return "n/a"
    return "pass" if parsed >= threshold else "fail"


def _summary_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    return {(row["file"], row["engine"]): row for row in rows}


def _method_order(rows: list[dict[str, str]], include_auto: bool) -> list[str]:
    methods: dict[str, None] = {}
    for row in rows:
        method = row["method"]
        if method == "auto" and not include_auto:
            continue
        methods.setdefault(method, None)
    return list(methods)


def _profile_name(
    file_name: str,
    method: str,
    summary_by_file_method: dict[tuple[str, str], dict[str, str]],
) -> str:
    row = summary_by_file_method.get((file_name, method), {})
    return row.get("pipeline", "")


def _browser_comparison_rows(
    browser_root: Path | None,
    expected_root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if browser_root is None:
        return [], []
    summary_path = browser_root / "summary.tsv"
    if not summary_path.exists():
        return [], []

    comparison_rows: list[dict[str, str]] = []
    summary_rows: list[dict[str, str]] = []
    for row in _read_tsv(summary_path):
        file_name = row["file"]
        match_percent = "n/a"
        matched_lines = ""
        total_lines = ""
        actual_path = browser_root / f"{file_name}.md"
        expected_path = expected_root / f"{file_name}.md"
        if row.get("exit") == "0" and actual_path.exists() and expected_path.exists():
            match_percent, matched_lines, total_lines = expected_match(
                result_body(actual_path),
                expected_path.read_text(encoding="utf-8", errors="replace"),
            )

        wall_seconds = "n/a"
        if row.get("wall_ms"):
            wall_seconds = f"{int(row['wall_ms']) / 1000:.3f}"

        profile_name = row.get("profile") or DEFAULT_BROWSER_TESSERACT_PROFILE
        comparison_rows.append(
            {
                "file": file_name,
                "method": BROWSER_TESSERACT_METHOD,
                "wall_seconds": wall_seconds,
                "match_percent": match_percent,
                "matched_expected_lines": matched_lines,
                "total_expected_lines": total_lines,
                "table_markdown_files": "0",
            }
        )
        summary_rows.append(
            {
                "file": file_name,
                "engine": BROWSER_TESSERACT_METHOD,
                "pipeline": profile_name,
                "flags": row.get("flags", ""),
            }
        )
    return comparison_rows, summary_rows


def build_tables(
    benchmark_root: Path,
    *,
    expected_root: Path = Path("debug/reference"),
    browser_root: Path | None = None,
    include_auto: bool = False,
) -> tuple[list[str], list[list[str]], list[str], list[list[str]]]:
    comparison_rows = _read_csv(benchmark_root / "comparison.csv")
    summary_rows = _read_tsv(benchmark_root / "summary.tsv")
    browser_comparison_rows, browser_summary_rows = _browser_comparison_rows(
        browser_root,
        expected_root,
    )
    comparison_rows.extend(browser_comparison_rows)
    summary_rows.extend(browser_summary_rows)

    summary_by_file_method = _summary_index(summary_rows)
    methods = _method_order(comparison_rows, include_auto)

    rows_by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in comparison_rows:
        if row["method"] == "auto" and not include_auto:
            continue
        rows_by_file[row["file"]].append(row)

    result_header = ["file", "threshold"]
    result_header.extend(f"{method} %" for method in methods)
    result_header.extend(f"{method} gate" for method in methods)
    result_header.extend(f"{method} profile" for method in methods)
    result_header.extend(f"{method} flags" for method in methods)

    time_header = ["file"]
    time_header.extend(f"{method} seconds" for method in methods)
    time_header.extend(f"{method} profile" for method in methods)
    time_header.extend(f"{method} flags" for method in methods)

    result_rows: list[list[str]] = []
    time_rows: list[list[str]] = []
    for file_name in sorted(rows_by_file):
        row_by_method = {row["method"]: row for row in rows_by_file[file_name]}
        threshold = _threshold_for_file(file_name)

        summary_rows_for_methods = [
            summary_by_file_method.get((file_name, method), {}) for method in methods
        ]
        profiles = [row.get("pipeline", "") for row in summary_rows_for_methods]
        flags = [
            _format_flags(method, profile, summary_row)
            for method, profile, summary_row in zip(
                methods, profiles, summary_rows_for_methods
            )
        ]
        result_row = [file_name, f"{threshold:.0f}"]
        result_row.extend(
            row_by_method.get(method, {}).get("match_percent", "n/a")
            for method in methods
        )
        result_row.extend(
            _gate(row_by_method.get(method, {}).get("match_percent", "n/a"), threshold)
            for method in methods
        )
        result_row.extend(profiles)
        result_row.extend(flags)

        time_row = [file_name]
        time_row.extend(
            row_by_method.get(method, {}).get("wall_seconds", "n/a")
            for method in methods
        )
        time_row.extend(profiles)
        time_row.extend(flags)

        result_rows.append(result_row)
        time_rows.append(time_row)

    return result_header, result_rows, time_header, time_rows


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build per-method debug quality/time CSV matrices."
    )
    parser.add_argument("--benchmark-root", required=True, type=Path)
    parser.add_argument("--browser-root", type=Path)
    parser.add_argument("--expected-root", default=Path("debug/reference"), type=Path)
    parser.add_argument("--output-root", default=Path("debug"), type=Path)
    parser.add_argument("--include-auto", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result_header, result_rows, time_header, time_rows = build_tables(
        args.benchmark_root,
        expected_root=args.expected_root,
        browser_root=args.browser_root,
        include_auto=args.include_auto,
    )
    _write_csv(args.output_root / "result.csv", result_header, result_rows)
    _write_csv(args.output_root / "time.csv", time_header, time_rows)
    print(f"Wrote debug CSV matrices to {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
