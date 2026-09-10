from types import SimpleNamespace

import pytest
from PIL import Image

from app.pipeline_config import OcrPipelineProfile
from app.pipeline_core.separated import SEPARATED_STAGES, SeparatedRecognition
from app.services import convert_service


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"dense_grid_fallback": True},
        {"lexical_correction": "t9_small"},
        {"structural_output": "records"},
    ],
)
@pytest.mark.parametrize("size", [(120, 80), (2400, 1600), (80, 8000)])
def test_service_preserves_rust_block_requests_and_final_output(monkeypatch, overrides, size):
    """Legacy page policies cannot rerun or rewrite the shared stage engine."""
    profile = OcrPipelineProfile(name="rust-route", **overrides)
    engine = object()
    jobs = tuple(SimpleNamespace(index=index, object_kind=0) for index in range(2))
    seen = []
    output = "# Heading\n\n| A | B |\n| --- | --- |\n| RAM 868 | 中文 |"

    def legacy_page_path(*_args, **_kwargs):
        pytest.fail("The host must not rerun legacy whole-page layout or repairs")

    for name in (
        "analyze_layout",
        "_recognize_dense_grid_page",
        "_apply_static_markdown_repairs",
        "_recover_search_results_screen",
    ):
        monkeypatch.setattr(convert_service, name, legacy_page_path)

    def recognize(crop, job, actual_engine, actual_profile, fallback):
        assert actual_engine is engine
        assert actual_profile is profile
        assert fallback is convert_service._recognize_text_with_sparse_fallback
        seen.append((job.index, crop.size, crop.getpixel((0, 0))))
        return SeparatedRecognition(f"block-{job.index}", 1000)

    def run_native(image, recognize_block):
        assert image.size == size
        for job in jobs:
            with Image.new("RGB", (16 + job.index, 12), (11, 22, 33)) as crop:
                assert recognize_block(crop, job).text == f"block-{job.index}"
        return output, jobs, SEPARATED_STAGES

    monkeypatch.setattr(convert_service, "recognize_separated_block", recognize)
    monkeypatch.setattr(convert_service, "run_native_separated_pipeline", run_native)
    with Image.new("RGB", size, "white") as image:
        markdown, metadata = convert_service._convert_page(image, engine, profile)

    assert markdown == output
    assert seen == [(0, (16, 12), (11, 22, 33)), (1, (17, 12), (11, 22, 33))]
    assert metadata["chunks"] == 2
    assert set(metadata["runtime_flags"]) == {
        "pipeline:rust_separated_v1",
        *(f"pipeline_stage:{stage}" for stage in SEPARATED_STAGES),
    }


def test_service_propagates_native_failure_without_python_layout_fallback(monkeypatch):
    def rejected(*_args, **_kwargs):
        raise ValueError("invalid native checkpoint")

    monkeypatch.setattr(convert_service, "run_native_separated_pipeline", rejected)
    with Image.new("RGB", (80, 60)) as image:
        with pytest.raises(ValueError, match="invalid native checkpoint"):
            convert_service._convert_page(image, object(), OcrPipelineProfile(name="rust-route"))


def test_service_counts_rust_tables_once_across_blocks_and_retry_jobs(monkeypatch):
    def job(object_id, rows, columns):
        return SimpleNamespace(
            object_kind=2,
            object_id=object_id,
            logical_row_count=rows,
            logical_column_count=columns,
        )

    jobs = (job(0, 3, 4), job(0, 3, 4), job(0, 2, 2), job(1, 2, 2))
    monkeypatch.setattr(
        convert_service, "run_native_separated_pipeline", lambda *_args: ("tables", jobs, SEPARATED_STAGES)
    )
    with Image.new("RGB", (100, 100)) as image:
        _, metadata = convert_service._convert_page(image, object(), OcrPipelineProfile(name="rust-route"))
    assert metadata["chunks"] == 4
    assert metadata["tables_found"] == 2
    assert metadata["table_cells"] == 16
