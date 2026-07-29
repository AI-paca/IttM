from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.geometry import GeometryAnalyzer
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter
from app.sparse_pipeline.synthetic_samples import (
    LINE_OWNER_ENCODING,
    LINE_OWNER_FRAME,
    LINE_OWNER_SUFFIX,
    SyntheticSpec,
    available_fonts,
    full_specs,
    render_sample,
    write_samples,
)


@pytest.mark.parametrize("language", ["en", "ru", "zh"])
def test_known_text_sample_preserves_the_exact_drawn_mask(language: str) -> None:
    try:
        font_path = available_fonts(language)[0]
    except FileNotFoundError:
        pytest.skip(f"no local {language} font")
    spec = SyntheticSpec(
        case_id=f"exact-{language}",
        language=language,
        layout="combined",
        line_spacing=0,
        margin_pt=0,
        border_pt=0,
        font_size=18,
        font_path=font_path,
        background_rgb=(246, 243, 235),
        foreground_rgb=(12, 13, 15),
    )
    sample = render_sample(spec)
    source_bytes = sample.image.tobytes()
    analyzer = GeometryAnalyzer()

    result = analyzer.analyze(sample.image)

    assert sample.image.tobytes() == source_bytes
    assert analyzer.last_bundle is not None
    assert result.alignment.correction_degrees == pytest.approx(0.0, abs=1e-9)
    np.testing.assert_array_equal(
        analyzer.last_bundle.foreground_mask, sample.expected_ink_mask
    )
    assert result.alignment.foreground_pixels == int(sample.expected_ink_mask.sum())


def test_full_grid_contains_more_than_six_hundred_cases_and_every_requested_axis() -> (
    None
):
    try:
        specs = full_specs()
    except FileNotFoundError:
        pytest.skip("multilingual fonts are unavailable")

    assert len(specs) == 1232
    assert {spec.language for spec in specs} == {"en", "ru", "zh", "mixed"}
    assert {spec.line_spacing for spec in specs} == set(range(11))
    assert {spec.margin_pt for spec in specs} == set(range(4))
    assert {spec.border_pt for spec in specs} == set(range(4))
    assert {spec.font_size for spec in specs}.issuperset({10, 18, 24})
    assert {spec.layout for spec in specs} == {"paragraph", "list", "table", "combined"}
    assert len({spec.font_path for spec in specs}) >= 4
    assert len({spec.case_id for spec in specs}) == len(specs)


def test_written_samples_persist_exact_line_and_frame_owner_oracle(
    tmp_path: Path,
) -> None:
    font_path = available_fonts("en")[0]
    spec = SyntheticSpec(
        case_id="owner-oracle",
        language="en",
        layout="list",
        line_spacing=0,
        margin_pt=1,
        border_pt=1,
        font_size=14,
        font_path=font_path,
    )
    sample = render_sample(spec)

    root = write_samples(tmp_path / "known", (spec,))

    owner_path = next(root.glob(f"*{LINE_OWNER_SUFFIX}"))
    with Image.open(owner_path) as opened:
        owner = np.asarray(opened, dtype=np.uint16)
    expected = np.zeros(sample.expected_ink_mask.shape, dtype=np.uint16)
    expected[sample.expected_frame_mask] = LINE_OWNER_FRAME
    for line_index, line_mask in enumerate(sample.expected_line_masks, start=1):
        expected[line_mask] = line_index
    np.testing.assert_array_equal(owner, expected)
    manifest = json.loads((root / "manifest.jsonl").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["line_owner_mask"] == owner_path.name
    assert manifest["line_owner_encoding"] == LINE_OWNER_ENCODING
    assert manifest["known_lines"] == len(sample.expected_line_masks)


def test_sample_corpus_publish_is_atomic_rollback_safe_and_never_overwrites(
    tmp_path: Path,
) -> None:
    font_path = available_fonts("en")[0]
    spec = SyntheticSpec(
        case_id="atomic",
        language="en",
        layout="paragraph",
        line_spacing=1,
        margin_pt=0,
        border_pt=0,
        font_size=12,
        font_path=font_path,
    )
    destination = tmp_path / "known"

    assert write_samples(destination, (spec,)) == destination.resolve()
    with pytest.raises(FileExistsError):
        write_samples(destination, (spec,))

    broken = SyntheticSpec(
        case_id="broken",
        language="en",
        layout="paragraph",
        line_spacing=1,
        margin_pt=0,
        border_pt=0,
        font_size=12,
        font_path=str(tmp_path / "missing-font.ttf"),
    )
    failed_destination = tmp_path / "failed"
    with pytest.raises(OSError):
        write_samples(failed_destination, (broken,))
    assert not failed_destination.exists()
    assert not tuple(tmp_path.glob(".failed.partial-*"))


def test_geometry_artifacts_are_complete_atomic_and_never_overwritten(
    tmp_path: Path,
) -> None:
    font_path = available_fonts("en")[0]
    sample = render_sample(
        SyntheticSpec(
            case_id="artifact",
            language="en",
            layout="list",
            line_spacing=5,
            margin_pt=1,
            border_pt=1,
            font_size=14,
            font_path=font_path,
        )
    )
    bundle = GeometryAnalyzer().analyze_bundle(sample.image)
    writer = GeometryArtifactWriter()

    run_dir = writer.write(tmp_path, run_id="sample", bundle=bundle)

    stage_dir = run_dir / "01-geometry"
    assert {path.name for path in stage_dir.iterdir()} == {
        "aligned.png",
        "alignment.json",
        "foreground-mask.png",
        "invariants.txt",
        "manifest.json",
        "matrix-overlay.png",
        "matrix.json",
        "matrix.txt",
        "nodes.jsonl",
        "ownership.png",
        "recursive-overlay.png",
        "rule-mask.png",
        "rules.jsonl",
        "segment-crops",
        "segments-overlay.png",
        "segments.jsonl",
        "source.png",
        "tree.txt",
    }
    manifest = json.loads((stage_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["semantic_stage"] == 1
    assert manifest["execution_step"] == 2
    assert manifest["foreground_pixels"] == int(sample.expected_ink_mask.sum())
    assert Image.open(stage_dir / "source.png").size == sample.image.size
    assert "ownership_exact=true" in (stage_dir / "invariants.txt").read_text(
        encoding="utf-8"
    )
    crop_manifest = json.loads(
        (stage_dir / "segment-crops" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert crop_manifest["segments"] == len(bundle.result.segmentation.segments)
    assert all(
        item["ownership_pixels"] == item["ink_pixels"]
        for item in crop_manifest["items"]
    )
    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="sample", bundle=bundle)
    assert not tuple(tmp_path.glob(".sample.partial-*"))
