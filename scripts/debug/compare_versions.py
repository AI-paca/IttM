#!/usr/bin/env python3
"""Compare OCR markdown outputs across benchmark versions (v1..v8).

For each version and engine we look for the produced ``<file>.md`` files across
all known output roots and compute the expected text-recall against the manual
references in ``debug/reference``.

Output is grouped by engine and reference file, then averaged.
"""
from __future__ import annotations

import importlib.util
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEBUG = ROOT / "debug"
REFERENCE_DIR = DEBUG / "reference"
TMP = DEBUG / "tmp"

ENGINES = ["tesseract", "easyocr", "browser-tesseract"]

# Roots that hold per-engine subdirectories. Each value is a list of candidate
# directories whose direct child <engine>/<file>.md files should be merged.
VERSION_ROOTS: dict[str, list[pathlib.Path]] = {
    "v1": [
        TMP / "full-pdf-native-v1",
        TMP / "full-pdf-as-png-v1",
        TMP / "image-fixtures-v1",
    ],
    "v2": [
        TMP / "full-pdf-native-v2",
        TMP / "full-pdf-as-png-v2",
        TMP / "image-fixtures-v2",
    ],
    "v3": [
        TMP / "full-pdf-native-v3",
        TMP / "full-pdf-as-png-v3",
        TMP / "image-fixtures-v3",
    ],
    "v4": [
        TMP / "full-pdf-native-v4",
        TMP / "full-pdf-as-png-v4",
        TMP / "image-fixtures-v4",
    ],
    "v5": [
        TMP / "full-pdf-native-v5",
        TMP / "full-pdf-as-png-v5",
        TMP / "image-fixtures-v5",
    ],
    # v6 (current run) used the flat per-engine layout produced by debug-all.sh.
    "v6": [TMP],
    "v7": [
        TMP / "full-pdf-native-v7",
        TMP / "full-pdf-as-png-v7",
        TMP / "image-fixtures-v7",
    ],
    "v8": [
        TMP / "full-pdf-native-v8",
        TMP / "full-pdf-as-png-v8",
        TMP / "image-fixtures-v8",
    ],
}


def _load_expected_match():
    """Import expected_match/result_body from scripts/debug/debug_report.py."""
    module_path = ROOT / "scripts" / "debug" / "debug_report.py"
    spec = importlib.util.spec_from_file_location("debug_report", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.expected_match, module.result_body


def _resolve_output(
    roots: list[pathlib.Path], engine: str, file_name: str
) -> pathlib.Path | None:
    """Find the first existing <root>/<engine>/<file>.md across roots."""
    for root in roots:
        candidate = root / engine / file_name
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    only_engine = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "all" else None

    expected_match, result_body = _load_expected_match()

    # Reference files we want to compare against.
    references = sorted(p.name for p in REFERENCE_DIR.glob("*.md"))
    if not references:
        print("no references in", REFERENCE_DIR, file=sys.stderr)
        return 1

    versions = ["v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"]
    engines = [e for e in ENGINES if only_engine is None or e == only_engine]

    # Per-file detailed table and per-version averages.
    print("=== per-file expected_match (%) — reference x engine x version ===")
    header = ["reference", "engine"] + versions
    print("\t".join(header))
    averages: dict[tuple[str, str], list[float]] = {}
    for ref_name in references:
        expected_text = (REFERENCE_DIR / ref_name).read_text(
            encoding="utf-8", errors="replace"
        )
        for engine in engines:
            row = [ref_name, engine]
            for version in versions:
                roots = VERSION_ROOTS[version]
                out = _resolve_output(roots, engine, ref_name)
                if out is None:
                    row.append("-")
                    continue
                actual = result_body(out)
                pct, matched, total = expected_match(actual, expected_text)
                if total and total != "0":
                    row.append(pct)
                    try:
                        averages[(version, engine)].append(float(pct))
                    except KeyError:
                        averages[(version, engine)] = [float(pct)]
                else:
                    row.append("n/a")
            print("\t".join(row))

    print()
    print("=== average expected_match (%) — engine x version ===")
    print("engine\t" + "\t".join(versions))
    for engine in engines:
        cells = []
        for version in versions:
            values = averages.get((version, engine))
            if values:
                cells.append(f"{statistics.mean(values):.1f}")
            else:
                cells.append("-")
        print(f"{engine}\t" + "\t".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
