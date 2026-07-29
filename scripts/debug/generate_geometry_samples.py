#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.synthetic_samples import (  # noqa: E402
    full_specs,
    smoke_specs,
    write_samples,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic known-text PNG corpus"
    )
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY_ROOT / "debug" / "generated"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    specs = smoke_specs() if args.mode == "smoke" else full_specs()
    output = write_samples(args.output.resolve() / args.run_id, specs)
    print(output)
    print(f"cases={len(specs)} mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
