#!/usr/bin/env python3
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
DOC_PATH = REPO_ROOT / "docs/en/backend-pipeline.md"

if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_flags import PIPELINE_OVERRIDE_SPECS  # noqa: E402


def main() -> int:
    documented = DOC_PATH.read_text(encoding="utf-8")
    missing: list[str] = []

    for key, spec in PIPELINE_OVERRIDE_SPECS.items():
        if f"`{key}`" not in documented:
            missing.append(key)
        missing.extend(
            f"{key}={mode}"
            for mode in sorted(spec.modes)
            if f"`{mode}`" not in documented
        )

    if missing:
        print("Undocumented public pipeline override or mode:")
        for token in missing:
            print(f"- {token}")
        return 1

    mode_count = sum(len(spec.modes) for spec in PIPELINE_OVERRIDE_SPECS.values())
    print(
        "Pipeline documentation covers "
        f"{len(PIPELINE_OVERRIDE_SPECS)} override keys and {mode_count} modes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
