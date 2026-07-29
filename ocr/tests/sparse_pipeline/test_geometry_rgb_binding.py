from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.sparse_pipeline.block_crops import BlockCropper
from app.sparse_pipeline.block_planning import BlockPlan, RecognitionBlock
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.geometry import GeometryAnalyzer
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter


_SEGMENT_ID = "segment-000000"
_OBJECT_ID = "object-000000"


def _png_bytes(pixels: np.ndarray, *, mode: str) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(pixels, mode=mode)
    try:
        image.save(output, format="PNG", compress_level=1, optimize=False)
    finally:
        image.close()
    return output.getvalue()


def _full_page_plan(width: int, height: int) -> BlockPlan:
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, width, height),
        core_segment_ids=(_SEGMENT_ID,),
        segment_ids=(_SEGMENT_ID,),
        context_segment_ids=(),
        object_ids=(_OBJECT_ID,),
    )
    return BlockPlan(
        aligned_size=(width, height),
        source_segment_ids=(_SEGMENT_ID,),
        blocks=(block,),
        adjacent_algebra=(),
    )


def test_geometry_result_rejects_noncanonical_aligned_rgb_sha256() -> None:
    result = GeometryAnalyzer().analyze(Image.new("RGB", (8, 8), "white"))

    for invalid in ("", "0" * 63, "0" * 65, "A" * 64, "g" * 64):
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            replace(result, aligned_rgb_sha256=invalid)


def test_crop_hash_uses_white_composited_canonical_rgb() -> None:
    rgba = np.array(
        [
            [
                (10, 20, 30, 0),
                (10, 30, 50, 128),
                (70, 80, 90, 255),
            ]
        ],
        dtype=np.uint8,
    )
    page = CropInput("rgba-page", _png_bytes(rgba, mode="RGBA"))

    crops, digest = BlockCropper().crop_with_rgb_sha256(
        page,
        aligned_size=(3, 1),
        plan=_full_page_plan(3, 1),
    )

    expected = np.array(
        [[(255, 255, 255), (132, 142, 152), (70, 80, 90)]],
        dtype=np.uint8,
    )
    assert digest == hashlib.sha256(expected.tobytes()).hexdigest()
    with Image.open(io.BytesIO(crops[0].raw.png_bytes)) as raw:
        assert raw.mode == "RGB"
        assert np.array_equal(np.asarray(raw), expected)


def test_crop_hash_is_exact_and_deterministic_across_internal_stripes() -> None:
    height, width = 1_025, 3
    pixels = (
        np.arange(height * width * 3, dtype=np.uint32).reshape(height, width, 3)
        % 251
    ).astype(np.uint8)
    page = CropInput("striped-page", _png_bytes(pixels, mode="RGB"))
    plan = _full_page_plan(width, height)
    cropper = BlockCropper()

    first_crops, first_digest = cropper.crop_with_rgb_sha256(
        page,
        aligned_size=(width, height),
        plan=plan,
    )
    second_crops, second_digest = cropper.crop_with_rgb_sha256(
        page,
        aligned_size=(width, height),
        plan=plan,
    )

    assert first_crops == second_crops
    assert first_digest == second_digest
    assert first_digest == hashlib.sha256(pixels.tobytes()).hexdigest()


def test_geometry_artifact_writer_rejects_bundle_digest_mismatch_atomically(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (24, 16), "white")
    ImageDraw.Draw(image).rectangle((3, 4, 18, 10), fill="black")
    bundle = GeometryAnalyzer().analyze_bundle(image)
    forged = replace(
        bundle,
        result=replace(bundle.result, aligned_rgb_sha256="0" * 64),
    )

    with pytest.raises(ValueError, match="supplied aligned RGB bundle"):
        GeometryArtifactWriter().write(tmp_path, run_id="mismatch", bundle=forged)

    assert not (tmp_path / "mismatch").exists()
    assert not tuple(tmp_path.glob(".mismatch.partial-*"))
