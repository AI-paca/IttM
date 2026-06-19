#!/usr/bin/env python3
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_flags import pipeline_flag_catalog  # noqa: E402

DOC_PATHS = (
    REPO_ROOT / "docs/ru/engine/README.md",
    REPO_ROOT / "debug/README.md",
)
SOURCE_FLAG_PATHS = (REPO_ROOT / "scripts/benchmark/benchmark-browser-ocr.ts",)
SOURCE_FLAG_RE = re.compile(r"['\"`]([a-z][a-z0-9_]*(?::[a-z][a-z0-9_]+)?)(?=[:=])")


def documented_key(documented: str, key: str) -> bool:
    if f"`{key}`" in documented:
        return True
    return f"`{key}:" in documented


def source_flag_keys() -> set[str]:
    keys: set[str] = set()
    for path in SOURCE_FLAG_PATHS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "flag" not in line:
                continue
            for match in SOURCE_FLAG_RE.finditer(line):
                keys.add(match.group(1))
    return keys


def main() -> int:
    documented = "\n".join(path.read_text(encoding="utf-8") for path in DOC_PATHS)
    catalog_keys = {entry["key"] for entry in pipeline_flag_catalog()}
    required = catalog_keys | source_flag_keys()
    missing = [key for key in sorted(required) if not documented_key(documented, key)]
    if missing:
        print("Undocumented pipeline flag keys:")
        for key in missing:
            print(f"- {key}")
        return 1
    print(f"Pipeline flag documentation covers {len(pipeline_flag_catalog())} keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
