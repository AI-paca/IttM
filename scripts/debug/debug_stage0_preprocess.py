#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPOSITORY_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.preprocessing import OcrPreprocessingPipeline  # noqa: E402

STAGE_DIRECTORY = "00-preprocess"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STEP_PROFILES = {
    "none": "stage0-none-v1",
    "projector_slide_dewarp": "stage0-projector-slide-dewarp-v1",
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rgb_sha256(image: Image.Image) -> str:
    if image.mode != "RGB":
        raise ValueError("RGB digest requires an RGB image")
    return hashlib.sha256(image.tobytes()).hexdigest()


def _load_rgb(path: Path) -> tuple[Image.Image, dict[str, object]]:
    with Image.open(path) as opened:
        encoded_format = opened.format
        encoded_mode = opened.mode
        encoded_size = list(opened.size)
        oriented = ImageOps.exif_transpose(opened)
        try:
            oriented.load()
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                alpha = rgba.getchannel("A")
                rgb = Image.new("RGB", rgba.size, "white")
                try:
                    rgb.paste(rgba, mask=alpha)
                finally:
                    alpha.close()
                    rgba.close()
            else:
                rgb = oriented.convert("RGB")
        finally:
            if oriented is not opened:
                oriented.close()

    metadata: dict[str, object] = {
        "path": str(path),
        "encoded_sha256": _file_sha256(path),
        "encoded_format": encoded_format,
        "encoded_mode": encoded_mode,
        "encoded_size": encoded_size,
        "decoded_rgb_size": list(rgb.size),
        "decoded_rgb_sha256": _rgb_sha256(rgb),
    }
    return rgb, metadata


def run_stage0(
    input_path: Path,
    output_root: Path,
    *,
    run_id: str,
    step: str,
) -> Path:
    """Run one explicit preprocessing profile and publish an immutable debug run."""

    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run_id contains unsafe characters")
    if step not in _STEP_PROFILES:
        choices = ", ".join(sorted(_STEP_PROFILES))
        raise ValueError(f"unknown stage 0 step {step!r}; choose one of: {choices}")

    source_path = input_path.resolve(strict=True)
    root = output_root.resolve()
    run_dir = root / run_id
    if run_dir.exists():
        raise FileExistsError(f"debug run already exists: {run_dir}")

    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
    stage_dir = temporary / STAGE_DIRECTORY
    stage_dir.mkdir()
    source_rgb: Image.Image | None = None
    processed: Image.Image | None = None
    output_rgb: Image.Image | None = None
    try:
        source_rgb, input_metadata = _load_rgb(source_path)
        step_names = () if step == "none" else (step,)
        pipeline = OcrPreprocessingPipeline.from_step_names(step_names)
        processed = pipeline.apply(source_rgb)
        output_rgb = processed if processed.mode == "RGB" else processed.convert("RGB")

        output_sha256 = _rgb_sha256(output_rgb)
        output_rgb.save(stage_dir / "raster.png", format="PNG")
        manifest = {
            "semantic_stage": 0,
            "execution_step": 1,
            "stage_name": "preprocess",
            "input": input_metadata,
            "step": step,
            "profile": {
                "name": _STEP_PROFILES[step],
                "image_preprocessing": list(step_names),
                "input_normalization": "exif-transpose-white-alpha-rgb",
            },
            "output": {
                "path": f"{STAGE_DIRECTORY}/raster.png",
                "mode": "RGB",
                "size": list(output_rgb.size),
            },
            "output_rgb_sha256": output_sha256,
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        try:
            temporary.rename(run_dir)
        except OSError as exc:
            if run_dir.exists():
                raise FileExistsError(f"debug run already exists: {run_dir}") from exc
            raise
        return run_dir
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if output_rgb is not None and output_rgb is not processed:
            output_rgb.close()
        if processed is not None and processed is not source_rgb:
            processed.close()
        if source_rgb is not None:
            source_rgb.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated preprocessing stage 0 on one raster image."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "stage0-preprocess",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--step",
        required=True,
        choices=tuple(_STEP_PROFILES),
        help="explicit preprocessing step; 'none' only normalizes the raster to RGB",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = run_stage0(
        args.input,
        args.output,
        run_id=args.run_id,
        step=args.step,
    )
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
