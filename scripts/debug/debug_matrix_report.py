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
from scripts.debug.debug_report import (  # noqa: E402
    MISSING_REFERENCE,
    NOT_APPLICABLE,
    NOT_CHECKED,
    result_body,
    scored_expected_match,
)

DEFAULT_THRESHOLD = 90.0
PDF_THRESHOLD = 90.0
THRESHOLD_OVERRIDES: dict[str, float] = {}

BROWSER_TESSERACT_METHOD = "browser-tesseract"
DEFAULT_BROWSER_TESSERACT_PROFILE = "browser_tesseract_dewarp"
RASTER_PAGE = re.compile(
    r"^(?P<pdf>.+\.pdf)\.page-(?P<page>\d{3})\.raster\.(?P<format>png|jpg)$",
    re.I,
)


def _float_or_none(value: str) -> float | None:
    if value in {"", "n/a", NOT_APPLICABLE, NOT_CHECKED, MISSING_REFERENCE}:
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
        if value in {NOT_APPLICABLE, NOT_CHECKED, MISSING_REFERENCE}:
            return value
        return NOT_CHECKED
    return "pass" if parsed >= threshold else "fail"


def _quality_gate(row: dict[str, str], threshold: float) -> str:
    success_gate = _gate(row.get("success_probability_percent", "n/a"), threshold)
    compact_gate = row.get("compact_quality_gate", NOT_CHECKED) or NOT_CHECKED
    grammar_gate = row.get("markdown_grammar_gate", NOT_CHECKED) or NOT_CHECKED
    if (
        success_gate == NOT_CHECKED
        and compact_gate == NOT_CHECKED
        and grammar_gate == NOT_CHECKED
    ):
        return _gate(row.get("match_percent", "n/a"), threshold)
    if success_gate == "fail" or compact_gate == "fail" or grammar_gate == "fail":
        return "fail"
    if success_gate == "pass" and compact_gate in {"pass", NOT_APPLICABLE} and grammar_gate in {"pass", NOT_APPLICABLE}:
        return "pass"
    if MISSING_REFERENCE in {success_gate, compact_gate, grammar_gate}:
        return MISSING_REFERENCE
    if NOT_CHECKED in {success_gate, compact_gate, grammar_gate}:
        return NOT_CHECKED
    return NOT_APPLICABLE


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


def _row_value(
    row_by_method: dict[str, dict[str, str]],
    method: str,
    key: str,
    default: str = NOT_CHECKED,
) -> str:
    row = row_by_method.get(method)
    if row is None:
        return default
    return row.get(key) or default


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
        text_match_percent = "n/a"
        compact_quality_percent = "n/a"
        compact_quality_gate = NOT_CHECKED
        lexical_t9_percent = "n/a"
        lexical_t9_gate = NOT_CHECKED
        markdown_grammar_percent = "n/a"
        markdown_grammar_gate = NOT_CHECKED
        markdown_grammar_notes = ""
        success_probability_percent = "n/a"
        success_probability_gate = NOT_CHECKED
        failure_kind = NOT_CHECKED
        actual_path = browser_root / f"{file_name}.md"
        expected_path = expected_root / f"{file_name}.md"
        if row.get("exit") == "0" and actual_path.exists() and expected_path.exists():
            match_score = scored_expected_match(
                result_body(actual_path),
                expected_path.read_text(encoding="utf-8", errors="replace"),
            )
            match_percent = match_score.match_percent
            matched_lines = match_score.matched_lines
            total_lines = match_score.total_lines
            text_match_percent = match_score.text_match_percent
            compact_quality_percent = match_score.compact_quality_percent
            compact_quality_gate = match_score.compact_quality_gate
            lexical_t9_percent = match_score.lexical_t9_percent
            lexical_t9_gate = match_score.lexical_t9_gate
            markdown_grammar_percent = match_score.markdown_grammar_percent
            markdown_grammar_gate = match_score.markdown_grammar_gate
            markdown_grammar_notes = match_score.markdown_grammar_notes
            success_probability_percent = match_score.success_probability_percent
            success_probability_gate = match_score.success_probability_gate
            failure_kind = match_score.failure_kind

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
                "text_match_percent": text_match_percent,
                "compact_quality_percent": compact_quality_percent,
                "compact_quality_gate": compact_quality_gate,
                "lexical_t9_percent": lexical_t9_percent,
                "lexical_t9_gate": lexical_t9_gate,
                "markdown_grammar_percent": markdown_grammar_percent,
                "markdown_grammar_gate": markdown_grammar_gate,
                "markdown_grammar_notes": markdown_grammar_notes,
                "success_probability_percent": success_probability_percent,
                "success_probability_gate": success_probability_gate,
                "failure_kind": failure_kind,
                "table_markdown_files": "0",
            }
        )
        summary_rows.append(
            {
                "file": file_name,
                "engine": BROWSER_TESSERACT_METHOD,
                "pipeline": profile_name,
                "flags": row.get("flags", ""),
                "exit": row.get("exit", ""),
            }
        )
    return comparison_rows, summary_rows


def _aggregate_name(file_name: str) -> str | None:
    match = RASTER_PAGE.fullmatch(file_name)
    if match is None:
        return None
    return f"{match.group('pdf')}.raster.{match.group('format').lower()}"


def _actual_markdown_path(
    benchmark_root: Path,
    browser_root: Path | None,
    method: str,
    file_name: str,
) -> Path | None:
    if method == BROWSER_TESSERACT_METHOD:
        return browser_root / f"{file_name}.md" if browser_root is not None else None
    return benchmark_root / method / f"{file_name}.md"


def _page_failure(
    summary: dict[str, str] | None,
    actual_path: Path | None,
) -> str | None:
    if summary is None:
        return "missing_summary"
    if "exit" in summary and summary.get("exit") != "0":
        return f"exit={summary.get('exit') or 'missing'}"
    if "curl_exit" in summary and summary.get("curl_exit") != "0":
        return f"curl_exit={summary.get('curl_exit') or 'missing'}"
    if "http_status" in summary and summary.get("http_status") != "200":
        return f"http_status={summary.get('http_status') or 'missing'}"
    if actual_path is None or not actual_path.is_file():
        return "missing_output"
    return None


def _aggregate_score_row(
    file_name: str,
    method: str,
    wall_seconds: str,
    actual_body: str,
    reference_body: str,
) -> dict[str, str]:
    score = scored_expected_match(actual_body, reference_body)
    return {
        "file": file_name,
        "method": method,
        "wall_seconds": wall_seconds,
        "match_percent": score.match_percent,
        "matched_expected_lines": score.matched_lines,
        "total_expected_lines": score.total_lines,
        "text_match_percent": score.text_match_percent,
        "compact_quality_percent": score.compact_quality_percent,
        "compact_quality_gate": score.compact_quality_gate,
        "lexical_t9_percent": score.lexical_t9_percent,
        "lexical_t9_gate": score.lexical_t9_gate,
        "markdown_grammar_percent": score.markdown_grammar_percent,
        "markdown_grammar_gate": score.markdown_grammar_gate,
        "markdown_grammar_notes": score.markdown_grammar_notes,
        "success_probability_percent": score.success_probability_percent,
        "success_probability_gate": score.success_probability_gate,
        "failure_kind": score.failure_kind,
        "table_markdown_files": "0",
    }


def _partial_aggregate_row(
    file_name: str,
    method: str,
    wall_seconds: str,
    failures: list[str],
) -> dict[str, str]:
    reason = "aggregate_partial: " + ", ".join(failures)
    return {
        "file": file_name,
        "method": method,
        "wall_seconds": wall_seconds,
        "match_percent": NOT_CHECKED,
        "matched_expected_lines": "",
        "total_expected_lines": "",
        "text_match_percent": NOT_CHECKED,
        "compact_quality_percent": NOT_CHECKED,
        "compact_quality_gate": NOT_CHECKED,
        "lexical_t9_percent": NOT_CHECKED,
        "lexical_t9_gate": NOT_CHECKED,
        "markdown_grammar_percent": NOT_CHECKED,
        "markdown_grammar_gate": NOT_CHECKED,
        "markdown_grammar_notes": reason,
        "success_probability_percent": NOT_CHECKED,
        "success_probability_gate": NOT_CHECKED,
        "failure_kind": reason,
        "table_markdown_files": "0",
    }


def _aggregate_raster_comparisons(
    comparison_rows: list[dict[str, str]],
    summary_rows: list[dict[str, str]],
    *,
    benchmark_root: Path,
    expected_root: Path,
    browser_root: Path | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    page_names_by_aggregate: dict[str, set[str]] = defaultdict(set)
    for row in comparison_rows:
        aggregate_name = _aggregate_name(row["file"])
        if aggregate_name is not None:
            page_names_by_aggregate[aggregate_name].add(row["file"])
    fixtures_root = benchmark_root / "fixtures"
    if fixtures_root.is_dir():
        for path in fixtures_root.iterdir():
            aggregate_name = _aggregate_name(path.name)
            if aggregate_name is not None:
                page_names_by_aggregate[aggregate_name].add(path.name)

    aggregate_families = {
        aggregate_name: tuple(
            sorted(
                page_names,
                key=lambda name: int(RASTER_PAGE.fullmatch(name).group("page")),
            )
        )
        for aggregate_name, page_names in page_names_by_aggregate.items()
        if (expected_root / f"{aggregate_name}.md").is_file()
        and not any(
            (expected_root / f"{page_name}.md").is_file()
            for page_name in page_names
        )
    }
    if not aggregate_families:
        return comparison_rows, summary_rows

    aggregate_page_names = {
        page_name
        for page_names in aggregate_families.values()
        for page_name in page_names
    }
    comparison_index = {
        (row["file"], row["method"]): row for row in comparison_rows
    }
    summary_index = _summary_index(summary_rows)
    methods_by_aggregate: dict[str, set[str]] = defaultdict(set)
    for row in comparison_rows:
        aggregate_name = _aggregate_name(row["file"])
        if aggregate_name in aggregate_families:
            methods_by_aggregate[aggregate_name].add(row["method"])

    aggregate_comparisons: list[dict[str, str]] = []
    aggregate_summaries: list[dict[str, str]] = []
    for aggregate_name, page_names in aggregate_families.items():
        reference_body = (expected_root / f"{aggregate_name}.md").read_text(
            encoding="utf-8",
            errors="replace",
        )
        for method in sorted(methods_by_aggregate[aggregate_name]):
            bodies: list[str] = []
            failures: list[str] = []
            elapsed = 0.0
            page_summaries: list[dict[str, str]] = []
            for page_name in page_names:
                page_match = RASTER_PAGE.fullmatch(page_name)
                page_label = f"page-{page_match.group('page')}"
                comparison = comparison_index.get((page_name, method))
                summary = summary_index.get((page_name, method))
                actual_path = _actual_markdown_path(
                    benchmark_root,
                    browser_root,
                    method,
                    page_name,
                )
                failure = _page_failure(summary, actual_path)
                if comparison is None and failure is None:
                    failure = "missing_comparison"
                if failure is not None:
                    failures.append(f"{page_label}({failure})")
                else:
                    bodies.append(result_body(actual_path).rstrip())
                if comparison is not None:
                    parsed_seconds = _float_or_none(
                        comparison.get("wall_seconds", "")
                    )
                    if parsed_seconds is not None:
                        elapsed += parsed_seconds
                if summary is not None:
                    page_summaries.append(summary)

            aggregate_body = "\n\n".join(bodies).rstrip() + "\n"
            candidate_path = _actual_markdown_path(
                benchmark_root,
                browser_root,
                method,
                aggregate_name,
            )
            if candidate_path is not None:
                candidate_path.write_text(aggregate_body, encoding="utf-8")
                status_path = candidate_path.with_suffix(
                    candidate_path.suffix + ".aggregate-status.txt"
                )
                status_path.write_text(
                    (
                        "status=partial\nmissing_pages="
                        + ",".join(failures)
                        + "\n"
                        if failures
                        else f"status=complete\npages={len(page_names)}\n"
                    ),
                    encoding="utf-8",
                )
            elapsed_text = f"{elapsed:.3f}"
            if failures:
                aggregate_comparisons.append(
                    _partial_aggregate_row(
                        aggregate_name,
                        method,
                        elapsed_text,
                        failures,
                    )
                )
            else:
                aggregate_comparisons.append(
                    _aggregate_score_row(
                        aggregate_name,
                        method,
                        elapsed_text,
                        aggregate_body,
                        reference_body,
                    )
                )

            profiles = {
                row.get("pipeline", "") for row in page_summaries
                if row.get("pipeline")
            }
            flags = {
                flag.strip()
                for row in page_summaries
                for flag in row.get("flags", "").split(";")
                if flag.strip()
            }
            aggregate_summaries.append(
                {
                    "file": aggregate_name,
                    "engine": method,
                    "pipeline": (
                        next(iter(profiles))
                        if len(profiles) == 1
                        else "aggregate:mixed"
                    ),
                    "flags": "; ".join(sorted(flags)),
                }
            )

    retained_comparisons = [
        row for row in comparison_rows if row["file"] not in aggregate_page_names
    ]
    retained_summaries = [
        row for row in summary_rows if row["file"] not in aggregate_page_names
    ]
    return (
        [*retained_comparisons, *aggregate_comparisons],
        [*retained_summaries, *aggregate_summaries],
    )


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
    comparison_rows, summary_rows = _aggregate_raster_comparisons(
        comparison_rows,
        summary_rows,
        benchmark_root=benchmark_root,
        expected_root=expected_root,
        browser_root=browser_root,
    )

    summary_by_file_method = _summary_index(summary_rows)
    methods = _method_order(comparison_rows, include_auto)

    rows_by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in comparison_rows:
        if row["method"] == "auto" and not include_auto:
            continue
        rows_by_file[row["file"]].append(row)

    result_header = ["file", "threshold"]
    result_header.extend(f"{method} %" for method in methods)
    result_header.extend(f"{method} text gate" for method in methods)
    result_header.extend(f"{method} compact quality %" for method in methods)
    result_header.extend(f"{method} compact quality gate" for method in methods)
    result_header.extend(f"{method} lexical t9 %" for method in methods)
    result_header.extend(f"{method} lexical t9 gate" for method in methods)
    result_header.extend(f"{method} markdown grammar %" for method in methods)
    result_header.extend(f"{method} markdown grammar gate" for method in methods)
    result_header.extend(f"{method} markdown grammar notes" for method in methods)
    result_header.extend(f"{method} success probability %" for method in methods)
    result_header.extend(f"{method} success probability gate" for method in methods)
    result_header.extend(f"{method} failure kind" for method in methods)
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
            _row_value(row_by_method, method, "match_percent")
            for method in methods
        )
        result_row.extend(
            _gate(_row_value(row_by_method, method, "match_percent"), threshold)
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "compact_quality_percent")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "compact_quality_gate")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "lexical_t9_percent")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "lexical_t9_gate")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "markdown_grammar_percent")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "markdown_grammar_gate")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "markdown_grammar_notes", "")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "success_probability_percent")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "success_probability_gate")
            for method in methods
        )
        result_row.extend(
            _row_value(row_by_method, method, "failure_kind")
            for method in methods
        )
        result_row.extend(
            _quality_gate(row_by_method.get(method, {}), threshold)
            for method in methods
        )
        result_row.extend(profiles)
        result_row.extend(flags)

        time_row = [file_name]
        time_row.extend(
            _row_value(row_by_method, method, "wall_seconds")
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
