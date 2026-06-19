#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with args.result.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))

    failures: list[str] = []
    for row in rows:
        file_name = row["file"]
        for column, value in row.items():
            if not column.endswith(" gate"):
                continue
            method = column.removesuffix(" gate")
            if value == "fail" or (args.strict_na and value == "n/a"):
                percent = row.get(f"{method} %", "n/a")
                threshold = row.get("threshold", "87")
                failures.append(f"{file_name}: {method}={percent}% < {threshold}%")

    if failures:
        print("Debug quality gate failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Debug quality gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
