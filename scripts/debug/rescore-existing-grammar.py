#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "scripts" / "debug" / "debug_report.py"
ENGINES = ("tesseract", "easyocr", "browser-tesseract")


@dataclass(frozen=True)
class Suite:
    name: str
    actual_root: Path
    reference_root: Path
    time_csv: Path


@dataclass(frozen=True)
class Job:
    suite: str
    file_name: str
    engine: str
    seconds: float
    actual_path: Path
    reference_path: Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-score existing OCR Markdown with the current grammar.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        required=True,
        metavar="NAME:ACTUAL_ROOT:REFERENCE_ROOT:TIME_CSV",
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--fastest", type=int, default=10)
    parser.add_argument("--failure-limit", type=int, default=30)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "ittm_debug_report",
        REPORT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {REPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _suite(value: str) -> Suite:
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise ValueError(
            "Suite must be NAME:ACTUAL_ROOT:REFERENCE_ROOT:TIME_CSV"
        )
    name, actual_root, reference_root, time_csv = parts
    return Suite(
        name=name,
        actual_root=Path(actual_root),
        reference_root=Path(reference_root),
        time_csv=Path(time_csv),
    )


def _seconds(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _jobs(suite: Suite) -> list[Job]:
    jobs = []
    with suite.time_csv.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            file_name = row.get("file", "")
            if not file_name:
                continue
            reference_path = suite.reference_root / f"{file_name}.md"
            if not reference_path.is_file():
                continue
            for engine in ENGINES:
                actual_path = suite.actual_root / engine / f"{file_name}.md"
                if not actual_path.is_file():
                    continue
                jobs.append(
                    Job(
                        suite=suite.name,
                        file_name=file_name,
                        engine=engine,
                        seconds=_seconds(row.get(f"{engine} seconds", "")),
                        actual_path=actual_path,
                        reference_path=reference_path,
                    )
                )
    return jobs


def _score(job: Job, report) -> dict[str, object]:
    actual = report.result_body(job.actual_path)
    expected = job.reference_path.read_text(
        encoding="utf-8",
        errors="replace",
    )
    percent, notes = report.score_markdown_structure(actual, expected)
    actual_lint = report.lint_markdown_controls(actual)
    reference_lint = report.lint_markdown_controls(expected)
    applicable = percent is not None
    passed = (
        applicable
        and percent >= 90.0
        and not actual_lint
        and not reference_lint
    )
    return {
        "suite": job.suite,
        "file": job.file_name,
        "engine": job.engine,
        "seconds": job.seconds,
        "grammar_percent": (
            f"{percent:.2f}" if percent is not None else "not_applicable"
        ),
        "gate": "pass" if passed else ("fail" if applicable else "n/a"),
        "actual_lint": "; ".join(actual_lint) or "pass",
        "reference_lint": "; ".join(reference_lint) or "pass",
        "notes": notes,
    }


def _print_summary(
    rows: list[dict[str, object]],
    fastest: int,
    failure_limit: int,
) -> None:
    print("Fastest checks:")
    for row in sorted(
        rows,
        key=lambda value: float(value["seconds"]),
    )[: max(0, fastest)]:
        print(
            f"  {float(row['seconds']):8.3f}s "
            f"{row['suite']:<10} {row['engine']:<18} "
            f"{row['grammar_percent']:>14} {row['gate']:<4} "
            f"{row['file']}"
        )

    print("\nPass rate:")
    keys = sorted(
        {
            (str(row["suite"]), str(row["engine"]))
            for row in rows
        }
    )
    for suite, engine in keys:
        selected = [
            row
            for row in rows
            if row["suite"] == suite and row["engine"] == engine
        ]
        applicable = [
            row
            for row in selected
            if row["grammar_percent"] != "not_applicable"
        ]
        passed = [row for row in applicable if row["gate"] == "pass"]
        ratio = 100.0 * len(passed) / max(1, len(applicable))
        print(
            f"  {suite:<10} {engine:<18} "
            f"{len(passed):>2}/{len(applicable):<2} {ratio:6.2f}%"
        )

    failures = [
        row
        for row in rows
        if row["gate"] == "fail"
    ]
    print(f"\nFailures: {len(failures)}")
    ordered_failures = sorted(
        failures,
        key=lambda value: (
            float(value["grammar_percent"]),
            str(value["suite"]),
            str(value["file"]),
            str(value["engine"]),
        ),
    )
    for row in ordered_failures[: max(0, failure_limit)]:
        print(
            f"  {row['grammar_percent']:>6}% "
            f"{row['suite']}/{row['engine']}: {row['file']}"
        )
    if len(ordered_failures) > failure_limit:
        print(
            f"  ... {len(ordered_failures) - failure_limit} more in CSV"
        )


def main() -> int:
    args = _parse_args()
    report = _load_report_module()
    jobs = sorted(
        (
            job
            for raw_suite in args.suite
            for job in _jobs(_suite(raw_suite))
        ),
        key=lambda job: job.seconds,
    )
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        rows = list(executor.map(lambda job: _score(job, report), jobs))

    _print_summary(rows, args.fastest, args.failure_limit)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {args.output}")
    return 1 if any(row["gate"] == "fail" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
