from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_geometry_corpus.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_debug_geometry_corpus_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mask_delta_counts_lost_added_and_xor_pixels(tmp_path: Path) -> None:
    module = _load_script()
    expected = np.asarray([[True, False], [False, True]], dtype=bool)
    actual = np.asarray([[True, True], [False, False]], dtype=bool)
    expected_path = tmp_path / "case.mask.png"
    Image.fromarray(expected.astype(np.uint8) * 255, mode="L").save(expected_path)

    values = module._mask_delta(expected_path, actual, require_exact_mask=True)

    assert values == (2, 1, 1, 2, "")


def test_mask_delta_requires_missing_mask_only_when_requested(tmp_path: Path) -> None:
    module = _load_script()
    missing = tmp_path / "missing.mask.png"
    actual = np.zeros((2, 2), dtype=bool)

    assert module._mask_delta(missing, actual, require_exact_mask=False) == (
        None,
        None,
        None,
        None,
        "",
    )
    required = module._mask_delta(missing, actual, require_exact_mask=True)
    assert required[:4] == (None, None, None, None)
    assert "required sibling mask is missing" in required[4]


def test_line_oracle_detects_cross_line_segments_without_pixel_loss(
    tmp_path: Path,
) -> None:
    module = _load_script()
    oracle = np.asarray(
        [
            [1, 1, 0, module.LINE_OWNER_FRAME],
            [1, 1, 0, module.LINE_OWNER_FRAME],
            [2, 2, 0, module.LINE_OWNER_FRAME],
            [2, 2, 0, module.LINE_OWNER_FRAME],
        ],
        dtype=np.uint16,
    )
    oracle_path = tmp_path / f"case{module.LINE_OWNER_SUFFIX}"
    Image.fromarray(oracle).save(oracle_path)
    ownership = np.full(oracle.shape, -1, dtype=np.int32)
    ownership[(oracle > 0) & (oracle < module.LINE_OWNER_FRAME)] = 7
    rules = oracle == module.LINE_OWNER_FRAME

    delta = module._line_oracle_delta(
        oracle_path,
        ownership,
        rules,
        require_line_oracle=True,
    )

    assert delta.line_count == 2
    assert delta.status == "exact"
    assert (delta.text_lost, delta.text_added) == (0, 0)
    assert (delta.rule_lost, delta.rule_added) == (0, 0)
    assert delta.cross_line_segments == 1
    assert delta.error == ""


def test_line_oracle_reports_text_rule_swaps_and_missing_required_file(
    tmp_path: Path,
) -> None:
    module = _load_script()
    oracle = np.asarray(
        [[1, 1, module.LINE_OWNER_FRAME], [2, 2, module.LINE_OWNER_FRAME]],
        dtype=np.uint16,
    )
    oracle_path = tmp_path / f"case{module.LINE_OWNER_SUFFIX}"
    Image.fromarray(oracle).save(oracle_path)
    ownership = np.asarray([[0, 0, 0], [1, -1, -1]], dtype=np.int32)
    rules = np.asarray([[False, False, False], [False, True, True]], dtype=bool)

    delta = module._line_oracle_delta(
        oracle_path,
        ownership,
        rules,
        require_line_oracle=True,
    )

    assert (delta.text_lost, delta.text_added) == (1, 1)
    assert (delta.rule_lost, delta.rule_added) == (1, 1)
    missing = module._line_oracle_delta(
        tmp_path / f"missing{module.LINE_OWNER_SUFFIX}",
        ownership,
        rules,
        require_line_oracle=True,
    )
    assert "required sibling line oracle is missing" in missing.error
    assert missing.status == "missing"


def test_line_oracle_allows_multiple_segments_for_one_line_and_rejects_bad_labels(
    tmp_path: Path,
) -> None:
    module = _load_script()
    oracle_path = tmp_path / f"split{module.LINE_OWNER_SUFFIX}"
    oracle = np.asarray([[1, 1, 0, 1, 1]], dtype=np.uint16)
    Image.fromarray(oracle).save(oracle_path)
    ownership = np.asarray([[3, 3, -1, 9, 9]], dtype=np.int32)
    delta = module._line_oracle_delta(
        oracle_path,
        ownership,
        np.zeros_like(oracle, dtype=bool),
        require_line_oracle=True,
    )
    assert delta.status == "exact"
    assert delta.cross_line_segments == 0

    bad_path = tmp_path / f"bad{module.LINE_OWNER_SUFFIX}"
    Image.fromarray(np.asarray([[1, 3]], dtype=np.uint16)).save(bad_path)
    invalid = module._line_oracle_delta(
        bad_path,
        np.asarray([[0, 1]], dtype=np.int32),
        np.zeros((1, 2), dtype=bool),
        require_line_oracle=True,
    )
    assert invalid.status == "invalid"
    assert "contiguous" in invalid.error


def test_legacy_reference_without_oracle_degrades_but_real_image_is_not_applicable(
    tmp_path: Path,
) -> None:
    module = _load_script()
    image = Image.new("RGB", (100, 40), "white")
    ImageDraw.Draw(image).rectangle((20, 12, 75, 24), fill="black")
    legacy = tmp_path / "legacy.png"
    image.save(legacy)
    legacy.with_suffix(".txt").write_text("known line\n", encoding="utf-8")
    default_output = tmp_path / "default-output"
    strict_output = tmp_path / "strict-output"

    default = module.run_item(
        str(legacy),
        str(tmp_path),
        str(default_output),
    )
    strict = module.run_item(
        str(legacy),
        str(tmp_path),
        str(strict_output),
        False,
        True,
    )

    assert default.status == "degraded"
    assert default.line_oracle_status == "unavailable"
    assert strict.status == "failed"
    assert strict.line_oracle_status == "missing"

    real = tmp_path / "real.png"
    image.save(real)
    real_result = module.run_item(
        str(real),
        str(tmp_path),
        str(tmp_path / "real-output"),
        False,
        True,
    )
    assert real_result.status == "complete"
    assert real_result.line_oracle_status == "not_applicable"


def test_generated_pdf_stack_is_expanded_only_after_exact_reconstruction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script()
    source = tmp_path / "case.pdf.raster.png"
    (tmp_path / "case.pdf").write_bytes(b"discovery-only")
    first = Image.new("RGB", (110, 50), "white")
    first.putpixel((4, 4), (0, 0, 0))
    second = Image.new("RGB", (100, 60), "white")
    second.putpixel((8, 8), (0, 0, 0))
    stacked = module._stack_pages((first, second), gap=32)
    stacked.save(source)
    stacked.close()

    def fake_render(_pdf: Path, *, dpi: int, max_pages: int):
        assert max_pages == 5
        if dpi == 72:
            return (Image.new("RGB", (36, 16), "white"),)
        if dpi == 220:
            return (first.copy(), second.copy())
        return (Image.new("RGB", (110, 50), "red"),)

    monkeypatch.setattr(module, "_render_pdf_pages", fake_render)
    output = tmp_path / "pages"
    output.mkdir()

    paths, record = module._expand_pdf_stack(
        source,
        tmp_path,
        output,
        dpi=0,
        max_pages=5,
        gap=32,
    )

    assert len(paths) == 2
    assert record["exact_stack_match"] is True
    assert record["dpi"] == 220
    with pytest.raises(ValueError, match="no longer matches"):
        module._expand_pdf_stack(
            source,
            tmp_path,
            output,
            dpi=300,
            max_pages=5,
            gap=32,
        )


def test_main_reports_all_statuses_and_returns_nonzero_on_failure(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    for name in ("complete.png", "degraded.png", "failed.png"):
        (input_root / name).write_bytes(b"discovery-only")
    (input_root / f"complete{module.LINE_OWNER_SUFFIX}").write_bytes(b"not-an-input-image")

    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: argparse.Namespace(
            input=input_root,
            output=output_root,
            run_id="statuses",
            workers=2,
            executor="thread",
            limit=None,
            require_exact_masks=True,
            require_exact_line_oracles=True,
            fail_on_degraded=False,
        ),
    )

    def fake_run_item(
        source: str,
        root: str,
        output: str,
        require_exact_mask: bool,
        require_line_oracle: bool,
    ):
        del root, output
        assert require_exact_mask is True
        assert require_line_oracle is True
        status = Path(source).stem
        return module.CorpusItem(
            source=Path(source).name,
            status=status,
            geometry_status="degraded" if status == "degraded" else "complete",
            elapsed_seconds=0.25,
            expected_pixels=10,
            lost_pixels=1 if status == "failed" else 0,
            added_pixels=0,
            xor_pixels=1 if status == "failed" else 0,
            known_lines=2,
            coarse_line_delta=1 if status == "degraded" else 0,
        )

    monkeypatch.setattr(module, "run_item", fake_run_item)

    assert module.main() == 1
    corpus_dir = output_root / "statuses"
    summary = json.loads((corpus_dir / "summary.json").read_text(encoding="utf-8"))
    markdown = (corpus_dir / "summary.md").read_text(encoding="utf-8")
    assert summary["status"] == "failed"
    assert (summary["complete"], summary["degraded"], summary["failures"]) == (1, 1, 1)
    for field in (
        "geometry_status",
        "expected_pixels",
        "lost_pixels",
        "added_pixels",
        "xor_pixels",
        "known_lines",
        "coarse_line_delta",
        "line_oracle_status",
        "line_oracle_lines",
        "cross_line_segments",
    ):
        assert field in markdown


def test_main_can_fail_the_interstage_gate_on_degraded_only(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    (input_root / "degraded.png").write_bytes(b"discovery-only")
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: argparse.Namespace(
            input=input_root,
            output=output_root,
            run_id="strict-degraded",
            workers=1,
            executor="thread",
            limit=None,
            require_exact_masks=True,
            require_exact_line_oracles=True,
            fail_on_degraded=True,
        ),
    )
    monkeypatch.setattr(
        module,
        "run_item",
        lambda *args: module.CorpusItem(
            source="degraded.png",
            status="degraded",
            geometry_status="complete",
            elapsed_seconds=0.1,
            known_lines=3,
            coarse_line_delta=1,
        ),
    )

    assert module.main() == 2
    summary = json.loads((output_root / "strict-degraded" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "degraded"
    assert summary["fail_on_degraded"] is True
