#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.preprocessing import OcrPreprocessingPipeline  # noqa: E402

BACKEND_COMPAT_STEPS = {
    "projector_slide_dewarp",
    "projected_document_dewarp",
}


def _bounded_size(
    width: int, height: int, *, max_dimension: int, max_pixels: int
) -> tuple[int, int]:
    scale = min(
        1.0,
        max_dimension / max(1, max(width, height)),
        (max_pixels / max(1, width * height)) ** 0.5,
    )
    return max(1, round(width * scale)), max(1, round(height * scale))


def _prepare_image(source: Path, output: Path, profile: dict) -> None:
    steps = tuple(
        step
        for step in profile.get("imagePreprocessing", [])
        if step in BACKEND_COMPAT_STEPS
    )
    max_dimension = int(profile.get("maxDimension") or 3200)
    max_pixels = int(profile.get("maxImagePixels") or 8_000_000)

    with Image.open(source) as raw_image:
        image = ImageOps.exif_transpose(raw_image).convert("RGB")

    try:
        pipeline = OcrPreprocessingPipeline.from_step_names(steps)
        processed = pipeline.apply(image)
        if processed is not image:
            image.close()
        image = processed

        if "browser_resize" in profile.get("imagePreprocessing", []):
            width, height = image.size
            target_size = _bounded_size(
                width,
                height,
                max_dimension=max_dimension,
                max_pixels=max_pixels,
            )
            if target_size != image.size:
                resample = getattr(Image, "Resampling", Image).LANCZOS
                resized = image.resize(target_size, resample)
                image.close()
                image = resized.convert("RGB")

        if "ocr_border" in profile.get("imagePreprocessing", []):
            bordered = ImageOps.expand(
                image,
                border=int(profile.get("ocrBorderPixels") or 10),
                fill="white",
            )
            image.close()
            image = bordered

        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG")
    finally:
        image.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare image fixtures for Node Tesseract.js debug OCR."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile-json", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = json.loads(args.profile_json.read_text(encoding="utf-8"))
    if args.source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, args.output)
        print(args.output)
        return 0

    _prepare_image(args.source, args.output, profile)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
