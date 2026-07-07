#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _methods(row: dict[str, str]) -> list[str]:
    return [column.removesuffix(" %") for column in row if column.endswith(" %")]


def _variant_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--variant must be NAME=ARTIFACT_DIR")
    name, path = value.split("=", 1)
    if not name.strip():
        raise argparse.ArgumentTypeError("variant name must not be empty")
    return name.strip(), Path(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge OCR battle variant result.csv files without best/average masking."
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        type=_variant_arg,
        help="Variant in NAME=ARTIFACT_DIR form. ARTIFACT_DIR must contain result.csv.",
    )
    parser.add_argument(
        "--required-methods",
        default="tesseract,easyocr,auto,browser-tesseract",
        help="Comma-separated methods required for an all-engine pass.",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    return parser.parse_args(argv)


def build_rows(
    variants: list[tuple[str, Path]],
    required_methods: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    long_rows: list[dict[str, str]] = []
    file_rows: list[dict[str, str]] = []

    for variant, artifact_dir in variants:
        result_path = artifact_dir / "result.csv"
        rows = _read_csv(result_path)
        for row in rows:
            file_name = row["file"]
            threshold = row.get("threshold", "90")
            methods = _methods(row)
            missing = [
                method
                for method in required_methods
                if f"{method} %" not in row or f"{method} gate" not in row
            ]
            failing = [
                method
                for method in required_methods
                if row.get(f"{method} gate", "n/a") != "pass"
            ]
            file_gate = "pass" if not missing and not failing else "fail"
            file_rows.append(
                {
                    "variant": variant,
                    "file": file_name,
                    "threshold": threshold,
                    "all_required_gate": file_gate,
                    "missing_required_methods": ";".join(missing),
                    "failing_required_methods": ";".join(failing),
                    "source_result": str(result_path),
                }
            )
            for method in methods:
                long_rows.append(
                    {
                        "variant": variant,
                        "file": file_name,
                        "method": method,
                        "threshold": threshold,
                        "percent": row.get(f"{method} %", "n/a"),
                        "gate": row.get(f"{method} gate", "n/a"),
                        "profile": row.get(f"{method} profile", ""),
                        "flags": row.get(f"{method} flags", ""),
                        "source_result": str(result_path),
                    }
                )

    return long_rows, file_rows


def _write_csv(path: Path, rows: list[dict[str, str]], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_summary(path: Path, file_rows: list[dict[str, str]]) -> None:
    lines = [
        "# OCR Battle Result",
        "",
        "Every value is copied from a variant `result.csv`; no best engine or average is computed.",
        "",
        "| Variant | File | All required gate | Failing methods | Source result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in file_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row['variant']}`",
                    f"`{row['file']}`",
                    f"`{row['all_required_gate']}`",
                    f"`{row['failing_required_methods'] or '-'}{row['missing_required_methods'] and ';missing:' + row['missing_required_methods'] or ''}`",
                    f"`{row['source_result']}`",
                ]
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.variant:
        raise SystemExit("At least one --variant is required")

    required_methods = [
        method.strip()
        for method in args.required_methods.split(",")
        if method.strip()
    ]
    long_rows, file_rows = build_rows(args.variant, required_methods)
    _write_csv(
        args.output,
        long_rows,
        [
            "variant",
            "file",
            "method",
            "threshold",
            "percent",
            "gate",
            "profile",
            "flags",
            "source_result",
        ],
    )
    file_output = args.output.with_name(f"{args.output.stem}.files.csv")
    _write_csv(
        file_output,
        file_rows,
        [
            "variant",
            "file",
            "threshold",
            "all_required_gate",
            "missing_required_methods",
            "failing_required_methods",
            "source_result",
        ],
    )
    if args.summary is not None:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        _write_summary(args.summary, file_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
