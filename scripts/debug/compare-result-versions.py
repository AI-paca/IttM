#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "debug" / "artifacts"
PHASES = ("full-pdf-native", "full-pdf-as-png", "image-fixtures")
ENGINES = ("tesseract", "easyocr", "browser-tesseract")


@dataclass(frozen=True)
class ScoreRow:
    version: str
    phase: str
    file: str
    engine: str
    text: float | None
    compact: float | None
    grammar: float | None
    t9: float | None
    weighted: float | None
    failure: str


def _number(value: str | None) -> float | None:
    if value in {None, "", "n/a", "not_checked"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _weighted(compact: float | None, grammar: float | None, t9: float | None) -> float | None:
    if compact is None or t9 is None:
        return None
    return 0.87 * compact + 0.09 * (100.0 if grammar is None else grammar) + 0.04 * t9


def _load(version: str) -> dict[tuple[str, str, str], ScoreRow]:
    rows: dict[tuple[str, str, str], ScoreRow] = {}
    for phase in PHASES:
        path = ARTIFACTS / f"{phase}-{version}" / "result.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for record in csv.DictReader(handle):
                for engine in ENGINES:
                    compact = _number(record.get(f"{engine} compact quality %"))
                    text = _number(record.get(f"{engine} %"))
                    if compact is None and text is None:
                        continue
                    grammar = _number(record.get(f"{engine} markdown grammar %"))
                    t9 = _number(record.get(f"{engine} lexical t9 %"))
                    row = ScoreRow(
                        version=version,
                        phase=phase,
                        file=record["file"],
                        engine=engine,
                        text=text,
                        compact=compact,
                        grammar=grammar,
                        t9=t9,
                        weighted=_weighted(compact, grammar, t9),
                        failure=record.get(f"{engine} failure kind", ""),
                    )
                    rows[(phase, row.file, engine)] = row
    return rows


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _averages(version: str, rows: dict[tuple[str, str, str], ScoreRow]) -> None:
    print(f"== weighted averages {version} ==")
    for phase in PHASES:
        cells: list[str] = []
        for engine in ENGINES:
            values = [
                row.weighted
                for row in rows.values()
                if row.phase == phase and row.engine == engine and row.weighted is not None
            ]
            cells.append(f"{engine}:{mean(values):.2f}" if values else f"{engine}:-")
        print(f"{phase}\t" + "\t".join(cells))


def _worst(rows: dict[tuple[str, str, str], ScoreRow], threshold: float, limit: int) -> None:
    print(f"\n== rows below {threshold:.0f} weighted ==")
    failing = [
        row
        for row in rows.values()
        if row.weighted is not None and row.weighted < threshold
    ]
    for row in sorted(failing, key=lambda item: item.weighted or 0.0)[:limit]:
        print(
            f"{row.weighted:6.2f}\t{row.phase}\t{row.engine}\t"
            f"text={_fmt(row.text)} compact={_fmt(row.compact)} "
            f"grammar={_fmt(row.grammar)} t9={_fmt(row.t9)}\t{row.failure}\t{row.file}"
        )


def _deltas(
    left: dict[tuple[str, str, str], ScoreRow],
    right: dict[tuple[str, str, str], ScoreRow],
    limit: int,
) -> None:
    common: list[tuple[float, ScoreRow, ScoreRow]] = []
    for key, right_row in right.items():
        left_row = left.get(key)
        if left_row is None or left_row.weighted is None or right_row.weighted is None:
            continue
        common.append((right_row.weighted - left_row.weighted, left_row, right_row))

    print("\n== worst weighted deltas ==")
    for delta, old, new in sorted(common, key=lambda item: item[0])[:limit]:
        print(
            f"{delta:7.2f}\t{old.weighted:6.2f}->{new.weighted:6.2f}\t"
            f"{new.phase}\t{new.engine}\t{new.file}"
        )

    print("\n== best weighted deltas ==")
    for delta, old, new in sorted(common, key=lambda item: item[0], reverse=True)[:limit]:
        print(
            f"{delta:7.2f}\t{old.weighted:6.2f}->{new.weighted:6.2f}\t"
            f"{new.phase}\t{new.engine}\t{new.file}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare existing debug result.csv artifacts with the weighted OCR formula.",
    )
    parser.add_argument("left", nargs="?", default="v7")
    parser.add_argument("right", nargs="?", default="v8")
    parser.add_argument("--threshold", type=float, default=90.0)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    left = _load(args.left)
    right = _load(args.right)
    if not left:
        raise SystemExit(f"no result.csv rows found for {args.left}")
    if not right:
        raise SystemExit(f"no result.csv rows found for {args.right}")

    _averages(args.left, left)
    print()
    _averages(args.right, right)
    _worst(right, args.threshold, args.limit)
    _deltas(left, right, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
