#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path

UNAVAILABLE_VALUES = {"", "n/a", "not_applicable", "not_checked", "missing_reference"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when per-method debug quality gates fail."
    )
    parser.add_argument("--result", default=Path("debug/result.csv"), type=Path)
    parser.add_argument(
        "--strict-na",
        action="store_true",
        help="Treat n/a gates as failures. By default n/a is allowed for unsupported method/file pairs.",
    )
    parser.add_argument(
        "--required-methods",
        default="",
        help=(
            "Comma-separated methods that must have percent and gate columns. "
            "Missing method columns fail the gate."
        ),
    )
    return parser.parse_args(argv)


def _percent_column_for_gate(row: dict[str, str], gate_label: str) -> str:
    for suffix in (
        " compact quality",
        " lexical t9",
        " markdown grammar",
        " success probability",
    ):
        if gate_label.endswith(suffix):
            return f"{gate_label.removesuffix(suffix)}{suffix} %"
    if gate_label.endswith(" text"):
        return f"{gate_label.removesuffix(' text')} %"
    success_probability_column = f"{gate_label} success probability %"
    if success_probability_column in row:
        return success_probability_column
    return f"{gate_label} %"


def _display_percent(row: dict[str, str], gate_label: str) -> str:
    value = row.get(_percent_column_for_gate(row, gate_label), "")
    return "missing_metric" if value in UNAVAILABLE_VALUES else value


def _display_label(row: dict[str, str], gate_label: str) -> str:
    if gate_label.endswith(" success probability"):
        return gate_label
    if _percent_column_for_gate(row, gate_label).endswith(" success probability %"):
        return f"{gate_label} overall"
    return gate_label


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with args.result.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))

    failures: list[str] = []
    required_methods = [
        method.strip() for method in args.required_methods.split(",") if method.strip()
    ]
    if rows and required_methods:
        columns = set(rows[0])
        for method in required_methods:
            if f"{method} %" not in columns or f"{method} gate" not in columns:
                failures.append(f"missing required method column: {method}")

    for row in rows:
        file_name = row["file"]
        for method in required_methods:
            if f"{method} %" not in row or f"{method} gate" not in row:
                failures.append(f"{file_name}: missing required method {method}")
        for column, value in row.items():
            if not column.endswith(" gate"):
                continue
            method = column.removesuffix(" gate")
            if value == "fail" or (args.strict_na and value == "n/a"):
                percent = _display_percent(row, method)
                label = _display_label(row, method)
                threshold = row.get("threshold", "87")
                failures.append(f"{file_name}: {label}={percent}% < {threshold}%")

    if failures:
        print("Debug quality gate failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Debug quality gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
