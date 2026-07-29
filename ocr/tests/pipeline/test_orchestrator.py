from dataclasses import asdict
import json

from app.pipeline_core import (
    PipelineCapabilities,
    RecognizedSegment,
    recipe_for,
    recognize_segments,
)


def test_recognition_stage_exposes_only_serializable_artifacts():
    batch = recognize_segments(
        [(0, 0, 10, 10), (0, 10, 10, 20)],
        lambda bbox: RecognizedSegment(
            kind="plain",
            bbox=bbox,
            parts=(f"segment-{bbox[1]}",),
            counters=(("chunks", 1),),
            flags=("ocr:test",),
        ),
    )

    payload = asdict(batch)
    encoded = json.dumps(payload, sort_keys=True)

    assert batch.total("chunks") == 2
    assert batch.trace.stage == "recognize_segments"
    assert batch.trace.input_count == batch.trace.output_count == 2
    assert "image" not in encoded
    assert "engine" not in encoded


def test_recognition_stage_preserves_sparse_structure_without_platform_handles():
    batch = recognize_segments(
        [object()],
        lambda _segment: RecognizedSegment(
            kind="sparse",
            bbox=(0, 0, 20, 10),
            parts=("value",),
            anchor=(2, 3),
            codes=((2, 4, 16),),
            list_marker=True,
            content_left=12,
        ),
    )

    assert batch.segments[0].anchor == (2, 3)
    assert batch.segments[0].codes == ((2, 4, 16),)


def test_trusted_api_recipe_bypasses_local_ocr_repairs():
    recipe = recipe_for(
        PipelineCapabilities(
            trusted_text=True,
            provides_layout=True,
            provides_markdown=True,
            needs_language_retry=False,
        )
    )

    assert recipe.stages == ("recognize_segments",)
    assert "select_language_candidate" not in recipe.stages
    assert "lexical_correction" not in recipe.stages
    assert "render_markdown" not in recipe.stages


def test_plain_api_text_keeps_only_required_structural_stages():
    recipe = recipe_for(
        PipelineCapabilities(
            trusted_text=True,
            provides_layout=True,
            provides_markdown=False,
            needs_language_retry=False,
        )
    )

    assert recipe.stages == (
        "recognize_segments",
        "group_structures",
        "render_markdown",
    )


def test_python_recipe_fallback_matches_core_contract(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline_core.native.native_pipeline_core",
        lambda: None,
    )

    recipe = recipe_for(PipelineCapabilities())

    assert recipe.stages == (
        "align",
        "segment",
        "project_sparse",
        "recognize_segments",
        "select_language_candidate",
        "lexical_correction",
        "group_structures",
        "render_markdown",
    )
