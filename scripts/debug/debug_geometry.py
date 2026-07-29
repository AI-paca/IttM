#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run geometry-only stage 1 on one raster image"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY_ROOT / "debug" / "tmp" / "geometry"
    )
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with Image.open(args.input) as opened:
        opened.load()
        bundle = GeometryAnalyzer().analyze_bundle(opened)
    run_dir = GeometryArtifactWriter().write(
        args.output.resolve(), run_id=args.run_id, bundle=bundle
    )
    result = bundle.result
    print(run_dir)
    print(
        f"foreground={result.alignment.foreground_pixels} "
        f"segments={len(result.segmentation.segments)} "
        f"rules={len(result.segmentation.rules)} cells={len(result.matrix.cells)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
