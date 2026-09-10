#!/usr/bin/env python3
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
DOC_PATH = REPO_ROOT / "docs/en/backend-pipeline.md"

if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_config import (  # noqa: E402
    DEFAULT_ENGINE_PIPELINE_PROFILES,
    OCR_PIPELINE_PROFILES,
)


def main() -> int:
    documented = DOC_PATH.read_text(encoding="utf-8")
    required = set(OCR_PIPELINE_PROFILES)
    required.update(DEFAULT_ENGINE_PIPELINE_PROFILES)
    required.update(DEFAULT_ENGINE_PIPELINE_PROFILES.values())

    missing = sorted(token for token in required if f"`{token}`" not in documented)
    if missing:
        print("Undocumented public engine or pipeline profile:")
        for token in missing:
            print(f"- {token}")
        return 1

    print(
        "Pipeline documentation covers "
        f"{len(DEFAULT_ENGINE_PIPELINE_PROFILES)} engines and "
        f"{len(OCR_PIPELINE_PROFILES)} selectable profiles."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
