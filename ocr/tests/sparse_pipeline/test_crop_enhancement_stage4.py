from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from app.sparse_pipeline import crop_enhancement
from app.sparse_pipeline.crop_enhancement import (
    GAMMA_DARK,
    RECIPE_ID,
    CropEnhancementBackendError,
    CropEnhancementConfig,
    CropEnhancementInvariantError,
    CropEnhancementLimitError,
    CropInput,
    EnhancedCrop,
    EnhancementBackend,
    EnhancementStatus,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.enhancement_artifacts import CropEnhancementArtifactWriter


def _png_bytes(array: np.ndarray, mode: str | None = None) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(array, mode=mode)
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _solid_png(
    size: tuple[int, int] = (8, 6),
    color: int | tuple[int, ...] = (255, 255, 255),
    mode: str = "RGB",
) -> bytes:
    output = io.BytesIO()
    image = Image.new(mode, size, color)
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _pixels(value: EnhancedCrop) -> np.ndarray:
    with Image.open(io.BytesIO(value.png_bytes)) as image:
        image.load()
        return np.array(image, copy=True)


def _enhance(
    payload: bytes,
    *,
    crop_id: str = "crop-000",
    config: CropEnhancementConfig | None = None,
) -> EnhancedCrop:
    return GammaDarkCropEnhancer(config).enhance(CropInput(crop_id, payload))


def test_frozen_recipe_matches_the_measured_kornia_float32_equations() -> None:
    source = np.array(
        (
            ((0, 0, 0), (255, 255, 255), (64, 64, 64), (255, 0, 0)),
            ((0, 255, 0), (0, 0, 255), (12, 34, 56), (240, 220, 200)),
        ),
        dtype=np.uint8,
    )

    result = _enhance(_png_bytes(source))

    assert GAMMA_DARK == 1.2
    assert RECIPE_ID == "kornia-gamma-dark-v1"
    assert result.recipe == RECIPE_ID
    assert result.gamma == GAMMA_DARK
    assert _pixels(result).tolist() == [[0, 255, 49, 60], [135, 19, 19, 218]]


def test_binary_text_geometry_and_pixels_are_exactly_preserved() -> None:
    source = np.full((17, 23, 3), 255, dtype=np.uint8)
    source[2:15, 3:5] = 0
    source[2:4, 3:18] = 0
    source[8:10, 3:15] = 0
    source[13:15, 3:18] = 0
    expected = source[:, :, 0]

    result = _enhance(_png_bytes(source))

    assert (result.width, result.height, result.mode) == (23, 17, "L")
    assert np.array_equal(_pixels(result), expected)
    assert np.count_nonzero(_pixels(result) == 0) == np.count_nonzero(expected == 0)


def test_neutral_antialiased_text_is_never_erased_or_lightened() -> None:
    ramp = np.arange(256, dtype=np.uint8).reshape(16, 16)
    source = np.repeat(ramp[:, :, None], 3, axis=2)

    result = _enhance(_png_bytes(source))
    output = _pixels(result)

    assert output[0, 0] == 0
    assert output[-1, -1] == 255
    assert np.all(output <= ramp)
    assert np.count_nonzero(output < 250) >= np.count_nonzero(ramp < 250)


def test_alpha_is_flattened_to_white_without_changing_crop_geometry() -> None:
    source = np.array(
        [[(0, 0, 0, 0), (0, 0, 0, 255), (0, 0, 0, 128), (255, 0, 0, 0)]],
        dtype=np.uint8,
    )

    result = _enhance(_png_bytes(source, mode="RGBA"))
    output = _pixels(result)[0]

    assert (result.width, result.height) == (4, 1)
    assert output[0] == 255
    assert output[1] == 0
    assert 0 < output[2] < 255
    assert output[3] == 255


@pytest.mark.parametrize(
    ("mode", "color"),
    (
        ("L", 181),
        ("RGB", (181, 181, 181)),
        ("RGBA", (181, 181, 181, 255)),
    ),
)
def test_uniform_background_stays_uniform_for_supported_png_modes(
    mode: str,
    color: int | tuple[int, ...],
) -> None:
    result = _enhance(_solid_png((11, 7), color, mode))
    output = _pixels(result)

    assert output.shape == (7, 11)
    assert np.unique(output).size == 1


def test_repeated_enhancement_is_byte_deterministic_and_keeps_source_immutable() -> None:
    source = np.arange(13 * 9 * 3, dtype=np.uint8).reshape(9, 13, 3)
    payload = _png_bytes(source)
    original = bytes(payload)
    enhancer = GammaDarkCropEnhancer()
    item = CropInput("deterministic", payload)

    first = enhancer.enhance(item)
    second = enhancer.enhance(item)

    assert payload == original
    assert first == second
    assert first.png_bytes == second.png_bytes
    assert first.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert first.output_sha256 == hashlib.sha256(first.png_bytes).hexdigest()


def test_batch_preserves_input_order_and_matches_individual_results() -> None:
    items = tuple(
        CropInput(f"crop-{index}", _solid_png((index + 2, index + 3), (40 * index,) * 3)) for index in range(1, 5)
    )
    enhancer = GammaDarkCropEnhancer()

    batch = enhancer.enhance_many(tuple(reversed(items)))

    assert tuple(value.crop_id for value in batch) == tuple(item.crop_id for item in reversed(items))
    assert batch == tuple(enhancer.enhance(item) for item in reversed(items))
    assert enhancer.enhance_many(()) == ()


def test_auto_backend_is_canonical_numpy_and_never_probes_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_probe(name: str) -> object:
        raise AssertionError(f"AUTO backend unexpectedly probed {name}")

    monkeypatch.setattr(crop_enhancement.importlib.util, "find_spec", forbidden_probe)

    result = _enhance(_solid_png())

    assert result.backend is EnhancementBackend.NUMPY


def test_explicit_missing_cuda_backend_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crop_enhancement.importlib.util, "find_spec", lambda name: None)
    config = CropEnhancementConfig(backend=EnhancementBackend.TORCH_CUDA)

    with pytest.raises(CropEnhancementBackendError, match="torch|CUDA|unavailable"):
        _enhance(_solid_png(), config=config)


def test_numpy_and_cuda_backends_are_byte_identical_when_cuda_is_available() -> None:
    if importlib.util.find_spec("torch") is None:
        pytest.skip("torch is not installed")
    import torch

    if not torch.cuda.is_available():
        pytest.skip("CUDA is not available to torch")
    rng = np.random.default_rng(20260719)
    payload = _png_bytes(rng.integers(0, 256, (37, 53, 3), dtype=np.uint8))

    numpy_result = _enhance(
        payload,
        config=CropEnhancementConfig(backend=EnhancementBackend.NUMPY),
    )
    cuda_result = _enhance(
        payload,
        config=CropEnhancementConfig(backend=EnhancementBackend.TORCH_CUDA),
    )

    assert cuda_result.png_bytes == numpy_result.png_bytes
    assert cuda_result.output_sha256 == numpy_result.output_sha256


@pytest.mark.parametrize("crop_id", ("", "../escape", "nested/path", "white space"))
def test_crop_input_rejects_unsafe_identifiers(crop_id: str) -> None:
    with pytest.raises(ValueError, match="crop_id|unsafe"):
        CropInput(crop_id, _solid_png())


def test_crop_input_requires_nonempty_immutable_bytes() -> None:
    with pytest.raises(ValueError, match="bytes"):
        CropInput("crop", bytearray(_solid_png()))
    with pytest.raises(ValueError, match="non-empty|bytes"):
        CropInput("crop", b"")


@pytest.mark.parametrize(
    "payload",
    (
        b"not a png",
        b"\x89PNG\r\n\x1a\ntruncated",
        b"\x89PNG\r\n\x1a\n" + b"not-valid-chunks" * 3,
    ),
)
def test_malformed_or_spoofed_png_fails_closed(payload: bytes) -> None:
    with pytest.raises(CropEnhancementInvariantError, match="PNG|invalid"):
        _enhance(payload)


def test_multiframe_apng_is_rejected() -> None:
    output = io.BytesIO()
    first = Image.new("RGB", (5, 4), "white")
    second = Image.new("RGB", (5, 4), "black")
    try:
        first.save(
            output,
            format="PNG",
            save_all=True,
            append_images=(second,),
            duration=100,
            loop=0,
        )
    finally:
        first.close()
        second.close()

    with pytest.raises(CropEnhancementInvariantError, match="frame"):
        _enhance(output.getvalue())


def test_unresolved_exif_orientation_is_rejected() -> None:
    output = io.BytesIO()
    image = Image.new("RGB", (5, 4), "white")
    exif = Image.Exif()
    exif[274] = 6
    try:
        image.save(output, format="PNG", exif=exif)
    finally:
        image.close()

    with pytest.raises(CropEnhancementInvariantError, match="orientation"):
        _enhance(output.getvalue())


def test_input_byte_limit_is_checked_before_png_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _solid_png((10, 10))
    config = CropEnhancementConfig(max_input_bytes=len(payload) - 1)

    def forbidden_open(*args: object, **kwargs: object) -> object:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(crop_enhancement.Image, "open", forbidden_open)

    with pytest.raises(CropEnhancementLimitError, match="byte"):
        _enhance(payload, config=config)


@pytest.mark.parametrize(
    "config",
    (
        CropEnhancementConfig(
            max_input_pixels=99,
            max_batch_pixels=99,
        ),
        CropEnhancementConfig(
            max_dimension=9,
        ),
    ),
)
def test_pixel_and_dimension_limits_are_checked_before_decode(
    config: CropEnhancementConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _solid_png((10, 10))

    def forbidden_load(*args: object, **kwargs: object) -> object:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)

    with pytest.raises(CropEnhancementLimitError, match="pixel|dimension"):
        _enhance(payload, config=config)


def test_aggregate_batch_budget_is_checked_before_any_pixel_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = (
        CropInput("first", _solid_png((10, 10))),
        CropInput("second", _solid_png((10, 10), (220, 220, 220))),
    )
    config = CropEnhancementConfig(
        max_input_pixels=100,
        max_batch_pixels=150,
    )

    def forbidden_load(*args: object, **kwargs: object) -> object:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)

    with pytest.raises(CropEnhancementLimitError, match="batch|pixel"):
        GammaDarkCropEnhancer(config).enhance_many(items)


def test_item_count_duplicate_ids_and_mutable_batch_fail_closed() -> None:
    first = CropInput("same", _solid_png())
    duplicate = CropInput("same", _solid_png(color=(220, 220, 220)))

    with pytest.raises(CropEnhancementLimitError, match="item"):
        GammaDarkCropEnhancer(CropEnhancementConfig(max_batch_items=1)).enhance_many((first, duplicate))
    with pytest.raises(CropEnhancementInvariantError, match="unique|identifier"):
        GammaDarkCropEnhancer().enhance_many((first, duplicate))
    with pytest.raises(CropEnhancementInvariantError, match="tuple|immutable"):
        GammaDarkCropEnhancer().enhance_many([first])


def test_output_byte_limit_raises_typed_error() -> None:
    config = CropEnhancementConfig(max_output_bytes=8)

    with pytest.raises(CropEnhancementLimitError, match="output|byte"):
        _enhance(_solid_png(), config=config)


@pytest.mark.parametrize(
    "changes",
    (
        {"backend": "numpy"},
        {"max_input_bytes": True},
        {"max_input_pixels": 0},
        {"max_dimension": -1},
        {"max_batch_items": 0},
        {"max_output_bytes": 0},
        {"dpi": 0},
        {"dpi": 2401},
        {"max_input_pixels": 10, "max_batch_pixels": 9},
    ),
)
def test_invalid_configuration_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CropEnhancementConfig(**changes)


def test_enhanced_crop_validates_hash_recipe_geometry_and_status() -> None:
    result = _enhance(_solid_png())

    with pytest.raises(ValueError, match="digest|hash"):
        replace(result, output_sha256="0" * 64)
    with pytest.raises(ValueError, match="dimension"):
        replace(result, width=0)
    with pytest.raises(ValueError, match="mode"):
        replace(result, mode="RGB")
    with pytest.raises(ValueError, match="recipe"):
        replace(result, recipe="different")
    with pytest.raises(ValueError, match="status"):
        replace(result, status="complete")
    with pytest.raises(ValueError, match="DPI|dpi|metadata"):
        replace(result, dpi=result.dpi + 1)

    with Image.open(io.BytesIO(result.png_bytes)) as opened:
        horizontal_dpi, vertical_dpi = opened.info["dpi"]
    assert horizontal_dpi == pytest.approx(result.dpi, abs=0.01)
    assert vertical_dpi == pytest.approx(result.dpi, abs=0.01)


def test_public_crop_payloads_are_deeply_immutable() -> None:
    item = CropInput("immutable", _solid_png())
    result = GammaDarkCropEnhancer().enhance(item)

    with pytest.raises(FrozenInstanceError):
        item.crop_id = "mutated"
    with pytest.raises(FrozenInstanceError):
        result.png_bytes = b"mutated"
    with pytest.raises(TypeError):
        result.png_bytes[0] = 0


def test_stage4_import_does_not_load_ocr_or_optional_gpu_runtimes() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.crop_enhancement
import app.sparse_pipeline.enhancement_artifacts
banned_roots = {
    'cv2', 'easyocr', 'kornia', 'onnxruntime', 'paddle', 'paddleocr',
    'pytesseract', 'tensorflow', 'tesserocr', 'torch',
}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
    or name.startswith('app.engines')
    or name.startswith('app.recognition')
)
print(json.dumps(banned))
raise SystemExit(bool(banned))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == []


def _artifact_files(stage_dir: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(stage_dir).as_posix() for path in stage_dir.rglob("*") if path.is_file()))


def test_debug_artifacts_are_atomic_deterministic_and_exact(tmp_path: Path) -> None:
    inputs = (
        CropInput("first", _solid_png((7, 5), (10, 20, 30))),
        CropInput("second", _solid_png((9, 6), (220, 210, 200))),
    )
    results = GammaDarkCropEnhancer().enhance_many(inputs)
    writer = CropEnhancementArtifactWriter()

    first_run = writer.write(
        tmp_path / "first-root",
        run_id="sample",
        inputs=inputs,
        results=results,
    )
    second_run = writer.write(
        tmp_path / "second-root",
        run_id="sample",
        inputs=inputs,
        results=results,
    )
    first_stage = first_run / "04-enhancement"
    second_stage = second_run / "04-enhancement"

    assert _artifact_files(first_stage) == (
        "crops.jsonl",
        "diagnostics.txt",
        "manifest.json",
        "output/first.png",
        "output/second.png",
        "source/first.png",
        "source/second.png",
    )
    assert _artifact_files(second_stage) == _artifact_files(first_stage)
    for relative in _artifact_files(first_stage):
        assert (first_stage / relative).read_bytes() == (second_stage / relative).read_bytes()
    for source, result in zip(inputs, results):
        assert (first_stage / "source" / f"{source.crop_id}.png").read_bytes() == (source.png_bytes)
        assert (first_stage / "output" / f"{source.crop_id}.png").read_bytes() == (result.png_bytes)

    manifest = json.loads((first_stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["semantic_stage"] == 4
    assert manifest["execution_step"] == 4
    assert manifest["status"] == "complete"
    assert manifest["crops"] == 2
    assert manifest["order"] == ["first", "second"]
    assert manifest["recipe"] == RECIPE_ID
    assert manifest["backend"] == EnhancementBackend.NUMPY.value
    assert manifest["candidate_only"] is True
    assert manifest["selection_deferred_to_stage2"] is True
    assert manifest["invariants"] == {
        "deterministic_order": True,
        "raw_source_preserved": True,
        "raw_source_retained": True,
        "same_geometry": True,
        "source_output_digests": True,
    }
    diagnostics = (first_stage / "diagnostics.txt").read_text(encoding="utf-8")
    assert "candidate_only=true\n" in diagnostics
    assert "selection_deferred_to_stage2=true\n" in diagnostics
    assert "raw_source_preserved=true\n" in diagnostics
    assert "raw_source_retained=true\n" in diagnostics
    entries = tuple(json.loads(line) for line in (first_stage / "crops.jsonl").read_text(encoding="utf-8").splitlines())
    assert tuple(entry["crop_id"] for entry in entries) == ("first", "second")
    for entry, source, result in zip(entries, inputs, results):
        assert entry["source_sha256"] == hashlib.sha256(source.png_bytes).hexdigest()
        assert entry["output_sha256"] == hashlib.sha256(result.png_bytes).hexdigest()
        assert [entry["width"], entry["height"]] == [result.width, result.height]
        assert entry["mode"] == "L"
        assert entry["recipe"] == RECIPE_ID
        assert entry["gamma"] == GAMMA_DARK
        assert entry["source"] == f"source/{source.crop_id}.png"
        assert entry["output"] == f"output/{source.crop_id}.png"


def test_stage4_is_not_integrated_into_the_legacy_or_default_pipeline() -> None:
    application_root = Path(__file__).resolve().parents[2] / "app"
    integrations = []
    for source in application_root.rglob("*.py"):
        if "sparse_pipeline" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        if "crop_enhancement" in text or "GammaDarkCropEnhancer" in text:
            integrations.append(source.relative_to(application_root).as_posix())

    assert integrations == []


def test_debug_writer_rejects_cross_input_metadata_mismatches(tmp_path: Path) -> None:
    inputs = (
        CropInput("first", _solid_png((7, 5))),
        CropInput("second", _solid_png((9, 6))),
    )
    results = GammaDarkCropEnhancer().enhance_many(inputs)
    writer = CropEnhancementArtifactWriter()

    with pytest.raises(ValueError, match="order|identical"):
        writer.write(
            tmp_path,
            run_id="wrong-order",
            inputs=inputs,
            results=tuple(reversed(results)),
        )
    with pytest.raises(ValueError, match="digest|source"):
        writer.write(
            tmp_path,
            run_id="wrong-source-hash",
            inputs=inputs,
            results=(replace(results[0], source_sha256="0" * 64), results[1]),
        )
    with pytest.raises(ValueError, match="geometry|size|metadata"):
        writer.write(
            tmp_path,
            run_id="wrong-source-size",
            inputs=inputs,
            results=(replace(results[0], width=results[0].width + 1), results[1]),
        )
    with pytest.raises(ValueError, match="backend"):
        writer.write(
            tmp_path,
            run_id="mixed-backend",
            inputs=inputs,
            results=(
                results[0],
                replace(results[1], backend=EnhancementBackend.TORCH_CUDA),
            ),
        )
    with pytest.raises(ValueError, match="DPI|dpi|metadata"):
        writer.write(
            tmp_path,
            run_id="mixed-dpi",
            inputs=inputs,
            results=(results[0], replace(results[1], dpi=301)),
        )


def test_enhanced_crop_rejects_output_png_with_forged_metadata(tmp_path: Path) -> None:
    source = CropInput("crop", _solid_png((7, 5)))
    result = GammaDarkCropEnhancer().enhance(source)
    different_output = _solid_png((1, 1), 0, "L")

    with pytest.raises(ValueError, match="output|geometry|size|mode|metadata"):
        replace(
            result,
            png_bytes=different_output,
            output_sha256=hashlib.sha256(different_output).hexdigest(),
        )

    assert not (tmp_path / "forged").exists()


def test_debug_writer_never_overwrites_and_rejects_unsafe_run_ids(
    tmp_path: Path,
) -> None:
    source = CropInput("crop", _solid_png())
    result = GammaDarkCropEnhancer().enhance(source)
    writer = CropEnhancementArtifactWriter()
    writer.write(tmp_path, run_id="sample", inputs=(source,), results=(result,))

    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="sample", inputs=(source,), results=(result,))
    assert not tuple(tmp_path.glob(".sample.partial-*"))
    for run_id in ("", "../escape", "nested/path", "white space"):
        with pytest.raises(ValueError, match="run_id|unsafe"):
            writer.write(
                tmp_path,
                run_id=run_id,
                inputs=(source,),
                results=(result,),
            )


def test_debug_writer_does_not_replace_a_broken_destination_symlink(
    tmp_path: Path,
) -> None:
    source = CropInput("crop", _solid_png())
    result = GammaDarkCropEnhancer().enhance(source)
    destination = tmp_path / "reserved"
    destination.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(FileExistsError):
        CropEnhancementArtifactWriter().write(
            tmp_path,
            run_id="reserved",
            inputs=(source,),
            results=(result,),
        )

    assert destination.is_symlink()
    assert not tuple(tmp_path.glob(".reserved.partial-*"))


def test_debug_writer_does_not_replace_a_concurrently_reserved_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = CropInput("crop", _solid_png())
    result = GammaDarkCropEnhancer().enhance(source)
    destination = tmp_path / "raced"
    original_mkdir = Path.mkdir
    injected = False

    def inject_race(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected
        if path == destination and not injected:
            original_mkdir(destination)
            injected = True
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", inject_race)

    with pytest.raises(FileExistsError):
        CropEnhancementArtifactWriter().write(
            tmp_path,
            run_id="raced",
            inputs=(source,),
            results=(result,),
        )

    assert injected is True
    assert destination.is_dir()
    assert not tuple(destination.iterdir())
    assert not tuple(tmp_path.glob(".raced.partial-*"))


def test_debug_publish_rolls_back_partial_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = CropInput("crop", _solid_png())
    result = GammaDarkCropEnhancer().enhance(source)
    writer = CropEnhancementArtifactWriter()

    def fail_after_partial_write(
        stage_dir: Path,
        *,
        inputs: tuple[CropInput, ...],
        results: tuple[EnhancedCrop, ...],
    ) -> None:
        del inputs, results
        (stage_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected artifact failure")

    monkeypatch.setattr(writer, "_write_stage", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="injected artifact failure"):
        writer.write(tmp_path, run_id="broken", inputs=(source,), results=(result,))

    assert not (tmp_path / "broken").exists()
    assert not tuple(tmp_path.glob(".broken.partial-*"))


def test_debug_writer_requires_immutable_ordered_tuples(tmp_path: Path) -> None:
    source = CropInput("crop", _solid_png())
    result = GammaDarkCropEnhancer().enhance(source)
    writer = CropEnhancementArtifactWriter()

    with pytest.raises(ValueError, match="tuple|immutable"):
        writer.write(tmp_path, run_id="list-input", inputs=[source], results=(result,))
    with pytest.raises(ValueError, match="tuple|immutable"):
        writer.write(tmp_path, run_id="list-result", inputs=(source,), results=[result])
