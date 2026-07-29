from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "debug"
    / "debug_stage0_preprocess.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "_debug_stage0_preprocess_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_none_writes_rgb_raster_and_bound_manifest(tmp_path: Path) -> None:
    module = _load_script()
    source = tmp_path / "source.png"
    expected = Image.new("RGB", (13, 7), (12, 34, 56))
    try:
        expected.save(source)
        expected_sha256 = hashlib.sha256(expected.tobytes()).hexdigest()
    finally:
        expected.close()

    run_dir = module.run_stage0(
        source,
        tmp_path / "runs",
        run_id="none-case",
        step="none",
    )
    stage_dir = run_dir / "00-preprocess"
    manifest = json.loads((stage_dir / "manifest.json").read_text(encoding="utf-8"))

    with Image.open(stage_dir / "raster.png") as raster:
        raster.load()
        assert raster.mode == "RGB"
        assert raster.size == (13, 7)
        assert hashlib.sha256(raster.tobytes()).hexdigest() == expected_sha256
    assert manifest["input"]["path"] == str(source.resolve())
    assert manifest["step"] == "none"
    assert manifest["profile"]["name"] == "stage0-none-v1"
    assert manifest["profile"]["image_preprocessing"] == []
    assert manifest["output_rgb_sha256"] == expected_sha256


def test_projector_step_is_explicit_and_records_transformed_rgb(
    tmp_path: Path,
) -> None:
    module = _load_script()
    source = tmp_path / "projector.png"
    image = Image.new("RGB", (960, 1280), (210, 220, 220))
    try:
        image.save(source)
    finally:
        image.close()

    run_dir = module.run_stage0(
        source,
        tmp_path / "runs",
        run_id="projector-case",
        step="projector_slide_dewarp",
    )
    stage_dir = run_dir / "00-preprocess"
    manifest = json.loads((stage_dir / "manifest.json").read_text(encoding="utf-8"))

    with Image.open(stage_dir / "raster.png") as raster:
        raster.load()
        assert raster.mode == "RGB"
        assert raster.size == (2000, 1200)
        raster_sha256 = hashlib.sha256(raster.tobytes()).hexdigest()
    assert manifest["step"] == "projector_slide_dewarp"
    assert manifest["profile"]["image_preprocessing"] == [
        "projector_slide_dewarp"
    ]
    assert manifest["output"]["size"] == [2000, 1200]
    assert manifest["output_rgb_sha256"] == raster_sha256


def test_cli_requires_explicit_step(tmp_path: Path) -> None:
    module = _load_script()

    with pytest.raises(SystemExit) as missing:
        module.parse_args(
            [
                str(tmp_path / "source.png"),
                "--output",
                str(tmp_path / "runs"),
                "--run-id",
                "missing-step",
            ]
        )

    assert missing.value.code == 2
